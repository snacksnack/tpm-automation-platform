"""Collect one program into one dated snapshot (RC1-301).

Every source the program names is read once, and every read lands in
`health` as `ok`, `missing` or `error` with a count and a reason. The
sections of the snapshot are filled only from sources that answered; a
source that raised leaves its section absent (`jira=None`) rather than
empty, and a source that answered with nothing is recorded as `missing` —
which, for the simulated program's Jira query, is the week-7 source break
seen the way the KPI tree wants it seen: as an absence, not as a zero.

Dates. `collected_at` is the wall clock. `sim_date` is read from the
simulator's clock when the program has one (RC1-299 writes `clock.json`
beside the spend line), and is the wall-clock date otherwise — so a real
program and the simulated one carry the same two stamps, and the KPI stage
computes against `sim_date` without knowing which kind it has.

Nothing here imports `simulate`: the clock file's two fields are read
directly, so the shipped package stays free of the development-only
simulator.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, date, datetime
from pathlib import Path

from collectors.jira import JiraCollector, JiraError
from collectors.models import (
    BillingRow,
    EvalRunRow,
    ProgramSnapshot,
    ProjectSnapshot,
    SourceHealth,
    SpendRow,
)
from collectors.programs import Program

# --- sources ----------------------------------------------------------------------------


def read_clock(clock_dir: str | Path) -> tuple[int, date] | None:
    """(sim_day, sim_date) from the simulator's clock.json, or None if not seeded."""
    path = Path(clock_dir) / "clock.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return int(data["day"]), date.fromisoformat(data["sim_date"])


def read_spend_csv(path: str | Path) -> list[SpendRow]:
    """The simulator's spend line: week, week_start, planned_usd, actual_usd, landed_on_day."""
    rows: list[SpendRow] = []
    with Path(path).open(newline="") as fh:
        for r in csv.DictReader(fh):
            landed = r.get("landed_on_day")
            rows.append(
                SpendRow(
                    week=int(r["week"]),
                    week_start=date.fromisoformat(r["week_start"]),
                    planned_usd=float(r["planned_usd"]),
                    actual_usd=float(r["actual_usd"]),
                    landed_on_day=int(landed) if landed not in (None, "") else None,
                )
            )
    return rows


def eval_run_row(record: dict) -> EvalRunRow:
    """Counts and cost from one stored run record (the JSON `agent_evals`
    writes), without importing agent_evals — the collector must not inherit
    the eval harness to read a table."""
    results = record.get("results", [])
    errored = sum(1 for r in results if r.get("error"))
    passed = sum(
        1
        for r in results
        if not r.get("error")
        and all(c["passed"] for c in r.get("characteristics", []) if not c.get("advisory"))
    )
    cost = sum(float(r.get("usage", {}).get("cost_usd", 0) or 0) for r in results)
    version = record["subject_version"]
    return EvalRunRow(
        run_id=record["run_id"],
        subject=version["subject"],
        code_version=version["code_version"],
        model=version.get("model"),
        started_at=datetime.fromisoformat(record["started_at"]),
        cases=len(results),
        passed=passed,
        errored=errored,
        cost_usd=cost,
    )


def read_eval_runs(dsn: str, subjects: tuple[str, ...] = ()) -> list[EvalRunRow]:
    """Every run row in the shared eval store, oldest first."""
    import psycopg2  # lazy: only the eval-store program needs a Postgres driver

    query = "SELECT record FROM eval_runs"
    params: tuple = ()
    if subjects:
        query += " WHERE subject = ANY(%s)"
        params = (list(subjects),)
    query += " ORDER BY started_at"
    conn = psycopg2.connect(dsn, sslmode="require")
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return [eval_run_row(row[0]) for row in cur.fetchall()]
    finally:
        conn.close()


# --- the collector ---------------------------------------------------------------------------


def collect_program(
    program: Program,
    *,
    jira: JiraCollector | None = None,
    eval_dsn: str | None = None,
    heroku_api_key: str | None = None,
    anthropic_admin_key: str | None = None,
    now: datetime | None = None,
) -> ProgramSnapshot:
    """One snapshot of `program`, every source's health recorded.

    `jira` is the collector to use when the program has a Jira source (a
    program with one and no collector records the source as an error —
    "not configured" is a reason, not a zero). `eval_dsn` likewise for the
    eval store. Nothing raises for a source problem; a caller reads `health`.
    """
    now = now or datetime.now(UTC)
    health: list[SourceHealth] = []
    sim_day: int | None = None
    sim_date = now.date()

    if program.clock_dir:
        try:
            clock = read_clock(program.clock_dir)
        except (OSError, ValueError, KeyError) as exc:
            clock = None
            health.append(SourceHealth(source="clock", status="error", detail=str(exc)))
        if clock is not None:
            sim_day, sim_date = clock
            health.append(
                SourceHealth(source="clock", status="ok", count=1, detail=f"day {sim_day}")
            )
        elif not any(h.source == "clock" for h in health):
            health.append(
                SourceHealth(
                    source="clock", status="missing",
                    detail=f"no clock.json in {program.clock_dir} — not seeded; using wall-clock",
                )
            )

    project: ProjectSnapshot | None = None
    if program.jira:
        if jira is None:
            health.append(
                SourceHealth(source="jira", status="error", detail="no Jira collector configured")
            )
        else:
            try:
                project = jira.collect(
                    program.jira.project, jql=program.jira.jql, with_changelog=False
                )
            except JiraError as exc:
                health.append(SourceHealth(source="jira", status="error", detail=str(exc)))
            else:
                n = len(project.issues)
                health.append(
                    SourceHealth(
                        source="jira",
                        status="ok" if n else "missing",
                        count=n,
                        detail=f"{n} issue(s) for {program.jira.jql!r}"
                        if n
                        else f"query returned no issues: {program.jira.jql!r}",
                    )
                )

    spend: list[SpendRow] = []
    if program.spend_csv:
        path = Path(program.spend_csv)
        if not path.exists():
            health.append(
                SourceHealth(source="spend", status="missing", detail=f"{path} does not exist")
            )
        else:
            try:
                spend = read_spend_csv(path)
            except (OSError, ValueError, KeyError) as exc:
                health.append(SourceHealth(source="spend", status="error", detail=f"{path}: {exc}"))
            else:
                health.append(
                    SourceHealth(
                        source="spend",
                        status="ok" if spend else "missing",
                        count=len(spend),
                        detail=f"{len(spend)} week(s) landed" if spend else "no weeks landed yet",
                    )
                )

    eval_runs: list[EvalRunRow] = []
    if program.eval_store:
        if not eval_dsn:
            health.append(
                SourceHealth(
                    source="eval-store", status="error", detail="EVAL_DATABASE_URL is not set"
                )
            )
        else:
            try:
                eval_runs = read_eval_runs(eval_dsn, program.eval_subjects)
            except Exception as exc:  # driver, network, auth: all "could not read"
                health.append(
                    SourceHealth(
                        source="eval-store", status="error",
                        detail=f"{type(exc).__name__}: {str(exc).strip()[:200]}",
                    )
                )
            else:
                health.append(
                    SourceHealth(
                        source="eval-store",
                        status="ok" if eval_runs else "missing",
                        count=len(eval_runs),
                        detail=f"{len(eval_runs)} run(s)" if eval_runs else "eval_runs is empty",
                    )
                )

    billing: list[BillingRow] = []
    if program.billing:
        from collectors import billing as billing_feeds  # httpx: only billing needs it here

        keys = {"anthropic-costs": anthropic_admin_key, "heroku-invoices": heroku_api_key}
        env_names = {"anthropic-costs": "ANTHROPIC_ADMIN_KEY", "heroku-invoices": "HEROKU_API_KEY"}
        readers = {
            "anthropic-costs": lambda key: billing_feeds.read_anthropic_costs(key, now=now),
            "heroku-invoices": billing_feeds.read_heroku_invoices,
        }
        for feed in program.billing:
            key = keys[feed]
            if not key:
                health.append(
                    SourceHealth(
                        source=feed, status="error",
                        detail=f"{env_names[feed]} is not set",
                    )
                )
                continue
            try:
                rows = readers[feed](key)
            except billing_feeds.BillingError as exc:
                health.append(SourceHealth(source=feed, status="error", detail=str(exc)[:300]))
            else:
                billing.extend(rows)
                health.append(
                    SourceHealth(
                        source=feed,
                        status="ok" if rows else "missing",
                        count=len(rows),
                        detail=f"{len(rows)} period(s) through {rows[-1].period_end}"
                        if rows
                        else "feed answered with no periods",
                    )
                )

    return ProgramSnapshot(
        program_id=program.id,
        collected_at=now,
        sim_date=sim_date,
        sim_day=sim_day,
        jira=project,
        spend=spend,
        eval_runs=eval_runs,
        billing=billing,
        health=health,
    )
