"""FastAPI entrypoint for the TPM automation platform.

Run locally:
    uvicorn main:app --reload

Endpoints:
    GET  /healthz          liveness probe (no auth, no side effects)
    POST /drift/run        run one drift-detection cycle (collect -> ... -> notify)
    GET  /drift/findings   the last stored run's findings (pure read, no side effects)
    GET  /drift/findings/{rule_type}/{downstream}   one finding, with its evidence

The read endpoints exist because `run_drift` is not a read: it collects from
Jira, writes rows, calls Anthropic, and posts to Slack. Anything that wants to
*look* at drift — a dashboard, a digest, an MCP tool — must not be doing all
that, least of all several times while someone explores a question (RC1-244).
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from config import settings
from drift.pipeline import run_drift
from store.models import Finding
from store.snapshot_store import SnapshotStore

# Emit the per-run structured JSON summary to stdout (captured by Fly logs),
# independent of uvicorn's own logging config.
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(message)s"))
_run_logger = logging.getLogger("drift.run")
_run_logger.setLevel(logging.INFO)
_run_logger.addHandler(_handler)
_run_logger.propagate = False

app = FastAPI(
    title="TPM Automation Platform",
    version="0.1.0",
    summary="Dependency Drift Detector and shared TPM collectors/store/narrative.",
)


class HealthResponse(BaseModel):
    status: str = "ok"


@app.get("/healthz", response_model=HealthResponse, tags=["ops"])
def healthz() -> HealthResponse:
    """Liveness probe used by CI, Fly.io, and the scheduler."""
    return HealthResponse()


@app.post("/drift/run", tags=["drift"])
def run_drift_endpoint(x_drift_token: str | None = Header(default=None)) -> dict:
    """Run one drift-detection cycle and return the run summary.

    Guarded by X-Drift-Token when DRIFT_RUN_TOKEN is set (the scheduler sends it);
    open when unset for local dev.
    """
    if settings.drift_run_token and x_drift_token != settings.drift_run_token:
        raise HTTPException(status_code=401, detail="invalid or missing X-Drift-Token")
    return run_drift()


# --- read-only findings -----------------------------------------------------
#
# These endpoints only ever SELECT. They never collect from Jira, never write a
# row, never call Anthropic, and never notify. That is the whole point: a caller
# can poll them as often as it likes without sending a single Slack message.

BUCKETS = ("red", "yellow", "white")


class FindingOut(BaseModel):
    """One finding, as stored.

    `detail` is returned verbatim. It already carries the dates and the change
    that triggered the rule ("RC1-159 due slipped 2026-07-09->2026-07-23 (14d);
    RC1-160 dates unchanged since."), and parsing that prose into fields would
    be fragile for no gain.
    """

    rule_type: str
    upstream: str | None
    downstream: str
    severity: float
    severity_bucket: str
    detail: str
    first_seen_run: int | None
    is_new: bool = Field(
        description="True when this finding was first detected in this run, not carried over."
    )

    @classmethod
    def of(cls, finding: Finding) -> FindingOut:
        return cls(
            rule_type=finding.rule_type,
            upstream=finding.upstream,
            downstream=finding.downstream,
            severity=finding.severity,
            severity_bucket=finding.severity_bucket,
            detail=finding.detail,
            first_seen_run=finding.first_seen_run,
            is_new=finding.is_new,
        )


class FindingsResponse(BaseModel):
    project_key: str
    run_id: int | None = Field(
        description="The stored run these findings came from. Null when none has run yet."
    )
    run_at: datetime | None = Field(
        description="When that run happened. This is not live data — quote this."
    )
    count: int
    findings: list[FindingOut]


def _store() -> SnapshotStore:
    return SnapshotStore(settings.db_path)


@app.get("/drift/findings", response_model=FindingsResponse, tags=["drift"])
def get_findings(
    project_key: str | None = Query(
        default=None, description="Defaults to the configured project."
    ),
    bucket: str | None = Query(default=None, description="red, yellow, or white."),
    rule: str | None = Query(default=None, description="Filter to one rule_type."),
    since_run: int | None = Query(
        default=None, description="Only findings first seen at or after this run id."
    ),
) -> FindingsResponse:
    """The findings from the most recent stored run. Pure read — no side effects.

    Reports the *last scheduled run*, not a fresh scan, which is why every
    response carries `run_id` and `run_at`.
    """
    if bucket is not None and bucket not in BUCKETS:
        raise HTTPException(
            status_code=422, detail=f"bucket must be one of {', '.join(BUCKETS)}"
        )

    key = project_key or settings.project_key
    store = _store()
    try:
        run = store.latest_run(key)
        if run is None:
            # No run yet is a valid state, not an error: the scheduler may simply
            # not have fired. An empty list with a null run says exactly that.
            return FindingsResponse(project_key=key, run_id=None, run_at=None, count=0, findings=[])

        findings = store.get_findings(run.run_id)
    finally:
        store.close()

    if bucket:
        findings = [f for f in findings if f.severity_bucket == bucket]
    if rule:
        findings = [f for f in findings if f.rule_type == rule]
    if since_run is not None:
        findings = [f for f in findings if (f.first_seen_run or 0) >= since_run]

    return FindingsResponse(
        project_key=key,
        run_id=run.run_id,
        run_at=run.created_at,
        count=len(findings),
        findings=[FindingOut.of(f) for f in findings],
    )


@app.get(
    "/drift/findings/{rule_type}/{downstream}",
    response_model=FindingsResponse,
    tags=["drift"],
)
def explain_finding(
    rule_type: str,
    downstream: str,
    upstream: str | None = Query(
        default=None, description="Required for rules that have a cause ticket."
    ),
    project_key: str | None = Query(default=None),
) -> FindingsResponse:
    """One finding and its evidence, addressed by identity.

    A finding is identified by `(rule_type, upstream, downstream)` — the same
    triple the store uses to carry `first_seen_run` across runs. Deliberately
    not a row id: findings are re-derived every run, so a row id would point at
    a different finding (or none) as soon as the scheduler fires again, and a
    caller that read a list and then asked about one entry would silently get
    the wrong answer.
    """
    key = project_key or settings.project_key
    store = _store()
    try:
        run = store.latest_run(key)
        findings = store.get_findings(run.run_id) if run else []
    finally:
        store.close()

    matched = [
        f
        for f in findings
        if f.rule_type == rule_type and f.downstream == downstream and f.upstream == upstream
    ]
    if not matched:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no current finding for rule {rule_type!r} on {downstream!r}"
                + (f" from {upstream!r}" if upstream else " with no upstream")
                + ". It may have cleared since the last run; "
                "GET /drift/findings lists what is open."
            ),
        )

    return FindingsResponse(
        project_key=key,
        run_id=run.run_id if run else None,
        run_at=run.created_at if run else None,
        count=len(matched),
        findings=[FindingOut.of(f) for f in matched],
    )
