"""Append-only SQLite snapshot store (RC1-135 [4/9]).

Persists a snapshot of issues/links per run so drift can be computed as a diff
over time — the difference between a linter ("these dates conflict today") and
a drift detector ("this conflict appeared Tuesday and nobody reacted").

Append-only: rows are only ever inserted, never updated or deleted (same
event-log pattern as the n8n email_log). Tables: runs, issue_snapshots,
link_snapshots, findings.

Note: an issue's changelog `date_changes` are NOT persisted — they're re-derived
from Jira each run — so snapshots reconstructed via load_previous() carry the
issue's field values (dates/status/links) but an empty date_changes list.

RC1-301 widens the same store rather than adding a second one: a run may
belong to a *program* and carry a sim-date, and beside the issue and link
tables sit the spend line, the eval store's run rows and a health row per
source. The Portfolio Console (RC1-233) and the KPI agent read the same
`runs` table — "one snapshot per run" holds because there is one place runs
live. Existing databases are migrated in place on open: new nullable columns
are added, nothing is rewritten, and the append-only rule is untouched.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from collectors.models import (
    BillingRow,
    DependencyLink,
    EvalRunRow,
    Issue,
    ProgramSnapshot,
    ProjectSnapshot,
    SourceHealth,
    SpendRow,
)
from store.models import Finding, ProgramRun, RunInfo

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS issue_snapshots (
    run_id          INTEGER NOT NULL REFERENCES runs(run_id),
    key             TEXT NOT NULL,
    summary         TEXT NOT NULL,
    status          TEXT NOT NULL,
    status_category TEXT NOT NULL,
    priority        TEXT,
    assignee_id     TEXT,
    assignee_name   TEXT,
    due             TEXT,
    start           TEXT
);
CREATE TABLE IF NOT EXISTS link_snapshots (
    run_id     INTEGER NOT NULL REFERENCES runs(run_id),
    upstream   TEXT NOT NULL,
    downstream TEXT NOT NULL,
    link_type  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
    run_id          INTEGER NOT NULL REFERENCES runs(run_id),
    rule_type       TEXT NOT NULL,
    upstream        TEXT,
    downstream      TEXT NOT NULL,
    severity        REAL NOT NULL,
    severity_bucket TEXT NOT NULL,
    detail          TEXT,
    first_seen_run  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS spend_snapshots (
    run_id        INTEGER NOT NULL REFERENCES runs(run_id),
    week          INTEGER NOT NULL,
    week_start    TEXT NOT NULL,
    planned_usd   REAL NOT NULL,
    actual_usd    REAL NOT NULL,
    landed_on_day INTEGER
);
CREATE TABLE IF NOT EXISTS eval_run_snapshots (
    run_id       INTEGER NOT NULL REFERENCES runs(run_id),
    eval_run_id  TEXT NOT NULL,
    subject      TEXT NOT NULL,
    code_version TEXT NOT NULL,
    model        TEXT,
    started_at   TEXT NOT NULL,
    cases        INTEGER NOT NULL,
    passed       INTEGER NOT NULL,
    errored      INTEGER NOT NULL,
    cost_usd     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS source_health (
    run_id INTEGER NOT NULL REFERENCES runs(run_id),
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    count  INTEGER NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS billing_snapshots (
    run_id       INTEGER NOT NULL REFERENCES runs(run_id),
    source       TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    amount_usd   REAL NOT NULL,
    kind         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_issue_run ON issue_snapshots(run_id);
CREATE INDEX IF NOT EXISTS idx_link_run ON link_snapshots(run_id);
CREATE INDEX IF NOT EXISTS idx_findings_identity
    ON findings(rule_type, upstream, downstream);
CREATE INDEX IF NOT EXISTS idx_spend_run ON spend_snapshots(run_id);
CREATE INDEX IF NOT EXISTS idx_eval_run ON eval_run_snapshots(run_id);
CREATE INDEX IF NOT EXISTS idx_health_run ON source_health(run_id);
CREATE INDEX IF NOT EXISTS idx_billing_run ON billing_snapshots(run_id);
"""

#: Columns added after a table first shipped. `CREATE TABLE IF NOT EXISTS`
#: leaves an existing table alone, so these are applied with ALTER TABLE on
#: open — nullable, so every existing row stays valid and unchanged.
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "runs": [("program_id", "TEXT"), ("sim_date", "TEXT"), ("sim_day", "INTEGER")],
    "issue_snapshots": [
        ("issue_type", "TEXT"), ("labels", "TEXT"), ("points", "REAL"),
        ("created", "TEXT"), ("parent", "TEXT"),
    ],
}


def _d(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


class SnapshotStore:
    def __init__(self, path: str | Path = ":memory:"):
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        for table, columns in _ADDED_COLUMNS.items():
            present = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns:
                if name not in present:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    def __enter__(self) -> SnapshotStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    # --- runs & snapshots ---------------------------------------------------
    def create_run(
        self,
        project_key: str,
        *,
        created_at: datetime | None = None,
        program_id: str | None = None,
        sim_date: date | None = None,
        sim_day: int | None = None,
    ) -> int:
        ts = (created_at or datetime.now(UTC)).isoformat()
        cur = self._conn.execute(
            "INSERT INTO runs (project_key, created_at, program_id, sim_date, sim_day) "
            "VALUES (?, ?, ?, ?, ?)",
            (project_key, ts, program_id, _iso(sim_date), sim_day),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def save_snapshot(self, run_id: int, snapshot: ProjectSnapshot) -> None:
        self._conn.executemany(
            "INSERT INTO issue_snapshots (run_id, key, summary, status, status_category, "
            "priority, assignee_id, assignee_name, due, start, "
            "issue_type, labels, points, created, parent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, i.key, i.summary, i.status, i.status_category,
                    i.priority, i.assignee_id, i.assignee_name, _iso(i.due), _iso(i.start),
                    i.issue_type, json.dumps(i.labels), i.points, _iso(i.created), i.parent,
                )
                for i in snapshot.issues
            ],
        )
        self._conn.executemany(
            "INSERT INTO link_snapshots (run_id, upstream, downstream, link_type) "
            "VALUES (?, ?, ?, ?)",
            [(run_id, link.upstream, link.downstream, link.link_type) for link in snapshot.links],
        )
        self._conn.commit()

    def load_previous(
        self, project_key: str, *, before_run: int | None = None
    ) -> ProjectSnapshot | None:
        """Most recent snapshot for the project (optionally before a given run)."""
        q = (
            "SELECT r.run_id FROM runs r WHERE r.project_key = ? "
            "AND EXISTS (SELECT 1 FROM issue_snapshots s WHERE s.run_id = r.run_id)"
        )
        params: list[object] = [project_key]
        if before_run is not None:
            q += " AND r.run_id < ?"
            params.append(before_run)
        q += " ORDER BY r.run_id DESC LIMIT 1"
        row = self._conn.execute(q, params).fetchone()
        return self._load_snapshot(project_key, int(row["run_id"])) if row else None

    def _load_snapshot(self, project_key: str, run_id: int) -> ProjectSnapshot:
        issues = [
            Issue(
                key=r["key"], summary=r["summary"], status=r["status"],
                status_category=r["status_category"], priority=r["priority"],
                assignee_id=r["assignee_id"], assignee_name=r["assignee_name"],
                due=_d(r["due"]), start=_d(r["start"]),
                issue_type=r["issue_type"], labels=json.loads(r["labels"] or "[]"),
                points=r["points"], created=_d(r["created"]), parent=r["parent"],
            )
            for r in self._conn.execute(
                "SELECT * FROM issue_snapshots WHERE run_id = ? ORDER BY key", (run_id,)
            )
        ]
        links = [
            DependencyLink(
                upstream=r["upstream"], downstream=r["downstream"], link_type=r["link_type"]
            )
            for r in self._conn.execute(
                "SELECT * FROM link_snapshots WHERE run_id = ? ORDER BY upstream, downstream",
                (run_id,),
            )
        ]
        return ProjectSnapshot(project_key=project_key, issues=issues, links=links)

    def latest_run(self, project_key: str) -> RunInfo | None:
        """The most recent run for a project, or None if it has never run.

        Every other run lookup here assumes the caller already holds a run id —
        `create_run` mints one, `previous_run_id` needs one to look behind. A
        read-only consumer has neither, so this is the entry point for "what is
        the current state of drift?" (RC1-244).
        """
        row = self._conn.execute(
            "SELECT run_id, project_key, created_at FROM runs WHERE project_key = ? "
            "ORDER BY run_id DESC LIMIT 1",
            (project_key,),
        ).fetchone()
        if row is None:
            return None
        return RunInfo(
            run_id=int(row["run_id"]),
            project_key=row["project_key"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def previous_run_id(self, project_key: str, before_run: int) -> int | None:
        """The most recent run for the project strictly before `before_run`."""
        row = self._conn.execute(
            "SELECT run_id FROM runs WHERE project_key = ? AND run_id < ? "
            "ORDER BY run_id DESC LIMIT 1",
            (project_key, before_run),
        ).fetchone()
        return int(row["run_id"]) if row else None

    # --- program snapshots (RC1-301) ------------------------------------------
    def save_program_snapshot(self, snapshot: ProgramSnapshot, *, project_key: str) -> int:
        """One run, every section, the health rows. Returns the run id.

        `jira=None` (the source errored) writes no issue rows; the health row
        is what says so. A reader that finds zero issue rows and an `error`
        health row knows the difference between "nothing there" and "could
        not look" — the distinction this store exists to keep.
        """
        run_id = self.create_run(
            project_key,
            created_at=snapshot.collected_at,
            program_id=snapshot.program_id,
            sim_date=snapshot.sim_date,
            sim_day=snapshot.sim_day,
        )
        if snapshot.jira is not None:
            self.save_snapshot(run_id, snapshot.jira)
        self._conn.executemany(
            "INSERT INTO spend_snapshots (run_id, week, week_start, planned_usd, actual_usd, "
            "landed_on_day) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (run_id, r.week, r.week_start.isoformat(), r.planned_usd, r.actual_usd,
                 r.landed_on_day)
                for r in snapshot.spend
            ],
        )
        self._conn.executemany(
            "INSERT INTO eval_run_snapshots (run_id, eval_run_id, subject, code_version, model, "
            "started_at, cases, passed, errored, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (run_id, r.run_id, r.subject, r.code_version, r.model, r.started_at.isoformat(),
                 r.cases, r.passed, r.errored, r.cost_usd)
                for r in snapshot.eval_runs
            ],
        )
        self._conn.executemany(
            "INSERT INTO billing_snapshots (run_id, source, period_start, period_end, "
            "amount_usd, kind) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (run_id, r.source, r.period_start.isoformat(), r.period_end.isoformat(),
                 r.amount_usd, r.kind)
                for r in snapshot.billing
            ],
        )
        self._conn.executemany(
            "INSERT INTO source_health (run_id, source, status, count, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            [(run_id, h.source, h.status, h.count, h.detail) for h in snapshot.health],
        )
        self._conn.commit()
        return run_id

    def program_runs(self, program_id: str) -> list[ProgramRun]:
        """Every run of a program, oldest first."""
        return [
            ProgramRun(
                run_id=int(r["run_id"]), program_id=r["program_id"],
                project_key=r["project_key"],
                collected_at=datetime.fromisoformat(r["created_at"]),
                sim_date=_d(r["sim_date"]), sim_day=r["sim_day"],
            )
            for r in self._conn.execute(
                "SELECT * FROM runs WHERE program_id = ? ORDER BY run_id", (program_id,)
            )
        ]

    def latest_program_run(
        self, program_id: str, *, sim_date: date | None = None
    ) -> ProgramRun | None:
        """The most recent run of a program, optionally the most recent *for a
        sim-date* — a day can be snapshotted more than once, and the last
        word is the one a recompute should use."""
        runs = [
            r for r in self.program_runs(program_id) if sim_date is None or r.sim_date == sim_date
        ]
        return runs[-1] if runs else None

    def load_program_snapshot(self, run_id: int) -> ProgramSnapshot:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None or row["program_id"] is None:
            raise KeyError(f"run {run_id} is not a program snapshot")
        health = [
            SourceHealth(source=h["source"], status=h["status"], count=h["count"],
                         detail=h["detail"] or "")
            for h in self._conn.execute(
                "SELECT * FROM source_health WHERE run_id = ? ORDER BY rowid", (run_id,)
            )
        ]
        jira_health = next((h for h in health if h.source == "jira"), None)
        project = None
        if jira_health is not None and jira_health.status != "error":
            project = self._load_snapshot(row["project_key"], run_id)
        spend = [
            SpendRow(week=r["week"], week_start=_d(r["week_start"]), planned_usd=r["planned_usd"],
                     actual_usd=r["actual_usd"], landed_on_day=r["landed_on_day"])
            for r in self._conn.execute(
                "SELECT * FROM spend_snapshots WHERE run_id = ? ORDER BY week", (run_id,)
            )
        ]
        eval_runs = [
            EvalRunRow(
                run_id=r["eval_run_id"], subject=r["subject"], code_version=r["code_version"],
                model=r["model"], started_at=datetime.fromisoformat(r["started_at"]),
                cases=r["cases"], passed=r["passed"], errored=r["errored"],
                cost_usd=r["cost_usd"],
            )
            for r in self._conn.execute(
                "SELECT * FROM eval_run_snapshots WHERE run_id = ? ORDER BY started_at", (run_id,)
            )
        ]
        billing = [
            BillingRow(
                source=r["source"], period_start=_d(r["period_start"]),
                period_end=_d(r["period_end"]), amount_usd=r["amount_usd"], kind=r["kind"],
            )
            for r in self._conn.execute(
                "SELECT * FROM billing_snapshots WHERE run_id = ? "
                "ORDER BY source, period_start",
                (run_id,),
            )
        ]
        return ProgramSnapshot(
            program_id=row["program_id"],
            collected_at=datetime.fromisoformat(row["created_at"]),
            sim_date=_d(row["sim_date"]),
            sim_day=row["sim_day"],
            jira=project,
            spend=spend,
            eval_runs=eval_runs,
            billing=billing,
            health=health,
        )

    # --- findings -----------------------------------------------------------
    def save_findings(self, run_id: int, findings: list[Finding]) -> list[Finding]:
        """Persist findings, stamping first_seen_run (carried forward by identity)."""
        saved: list[Finding] = []
        for f in findings:
            first_seen = self._existing_first_seen(f) or run_id
            self._conn.execute(
                "INSERT INTO findings (run_id, rule_type, upstream, downstream, severity, "
                "severity_bucket, detail, first_seen_run) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, f.rule_type, f.upstream, f.downstream, f.severity,
                 f.severity_bucket, f.detail, first_seen),
            )
            saved.append(f.model_copy(update={"run_id": run_id, "first_seen_run": first_seen}))
        self._conn.commit()
        return saved

    def _existing_first_seen(self, f: Finding) -> int | None:
        # `IS` is NULL-safe, so a null upstream matches correctly.
        row = self._conn.execute(
            "SELECT MIN(first_seen_run) AS fs FROM findings "
            "WHERE rule_type IS ? AND upstream IS ? AND downstream IS ?",
            (f.rule_type, f.upstream, f.downstream),
        ).fetchone()
        return row["fs"]

    def get_findings(self, run_id: int) -> list[Finding]:
        return [
            Finding(
                rule_type=r["rule_type"], upstream=r["upstream"], downstream=r["downstream"],
                severity=r["severity"], severity_bucket=r["severity_bucket"],
                detail=r["detail"] or "",
                run_id=r["run_id"], first_seen_run=r["first_seen_run"],
            )
            for r in self._conn.execute(
                "SELECT * FROM findings WHERE run_id = ? ORDER BY severity DESC", (run_id,)
            )
        ]
