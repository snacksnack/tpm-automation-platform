"""Turn structured drift findings into a TPM-voiced digest (RC1-138 [7/9]).

Claude communicates; the rules engine decides. We hand the model the exact facts
(keys, dates, owners, severity buckets) and a strict prompt, and it narrates. The
model never invents findings or re-ranks severity — it only writes prose.

Design notes:
- The prompt lives in a versioned template file (templates/drift_digest.md), not
  inline strings.
- Empty findings short-circuit to a one-line all-clear WITHOUT calling the API —
  no fabricated report, no spend.
- Structured output (output_config json_schema) so [8/9] can route pieces.
- Explicit timeout + bounded retries on the API client (per the n8n lessons).
"""

from __future__ import annotations

import json
from pathlib import Path

from collectors.models import ProjectSnapshot
from config import settings
from narrative.models import DigestLine, DriftDigest
from store.models import Finding

MODEL = "claude-opus-4-8"
_TEMPLATE = Path(__file__).parent / "templates" / "drift_digest.md"

# json_schema for structured output — every object closed, every field required.
_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "downstream": {"type": "string"},
                    "bucket": {"type": "string"},
                    "line": {"type": "string"},
                },
                "required": ["downstream", "bucket", "line"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["subject", "summary", "findings"],
    "additionalProperties": False,
}


class NarrativeError(RuntimeError):
    pass


def load_prompt() -> str:
    return _TEMPLATE.read_text()


def _iso(value: object) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else None


def _ticket(key: str | None, snapshot: ProjectSnapshot) -> dict | None:
    if key is None:
        return None
    issue = snapshot.by_key.get(key)
    if issue is None:
        return {"key": key}
    return {
        "key": issue.key,
        "summary": issue.summary,
        "status": issue.status,
        "owner": issue.assignee_name,
        "due": _iso(issue.due),
        "start": _iso(issue.start),
    }


def _is_new(f: Finding) -> bool:
    # Persisted findings carry run_id/first_seen; a fresh (unpersisted) finding is new.
    return True if f.run_id is None else f.is_new


def build_payload(
    findings: list[Finding],
    snapshot: ProjectSnapshot,
    resolved: list[Finding] | None = None,
) -> dict:
    """The exact facts handed to the model — no derived judgments."""
    ordered = sorted(findings, key=lambda f: f.severity, reverse=True)
    counts = {"red": 0, "yellow": 0, "white": 0, "new": 0}
    for f in findings:
        counts[f.severity_bucket] = counts.get(f.severity_bucket, 0) + 1
        if _is_new(f):
            counts["new"] += 1
    return {
        "project": snapshot.project_key,
        "counts": counts,
        "findings": [
            {
                "rule": f.rule_type,
                "severity": f.severity,
                "bucket": f.severity_bucket,
                "is_new": _is_new(f),
                "detail": f.detail,
                "upstream": _ticket(f.upstream, snapshot),
                "downstream": _ticket(f.downstream, snapshot),
            }
            for f in ordered
        ],
        "resolved": [
            {"downstream": f.downstream, "rule": f.rule_type, "upstream": f.upstream}
            for f in (resolved or [])
        ],
    }


def _all_clear(snapshot: ProjectSnapshot, resolved: list[Finding] | None) -> DriftDigest:
    extra = f" {len(resolved)} finding(s) resolved since last run." if resolved else ""
    return DriftDigest(
        subject=f"{snapshot.project_key} dependency drift: all clear",
        summary=f"No dependency drift detected in {snapshot.project_key}.{extra}",
        findings=[],
        all_clear=True,
    )


def _default_client():
    import anthropic  # imported lazily so tests/offline paths need no SDK auth

    return anthropic.Anthropic(
        api_key=settings.anthropic_api_key or None,
        timeout=60.0,
        max_retries=3,
    )


def _generate(client, model: str, system: str, payload: dict) -> dict:
    resp = client.messages.create(
        model=model,
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": json.dumps(payload)}],
        output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
    )
    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
    if text is None:
        raise NarrativeError("model returned no text block")
    return json.loads(text)


def build_digest(
    findings: list[Finding],
    snapshot: ProjectSnapshot,
    *,
    resolved: list[Finding] | None = None,
    client=None,
    model: str = MODEL,
) -> DriftDigest:
    """Findings -> TPM-voiced digest. Empty findings never call the API."""
    if not findings:
        return _all_clear(snapshot, resolved)

    payload = build_payload(findings, snapshot, resolved)
    data = _generate(client or _default_client(), model, load_prompt(), payload)
    return DriftDigest(
        subject=data["subject"],
        summary=data["summary"],
        findings=[DigestLine(**line) for line in data["findings"]],
        all_clear=False,
    )
