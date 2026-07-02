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
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from collectors.models import DependencyLink, Issue, ProjectSnapshot
from store.models import Finding

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
CREATE INDEX IF NOT EXISTS idx_issue_run ON issue_snapshots(run_id);
CREATE INDEX IF NOT EXISTS idx_link_run ON link_snapshots(run_id);
CREATE INDEX IF NOT EXISTS idx_findings_identity
    ON findings(rule_type, upstream, downstream);
"""


def _d(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


class SnapshotStore:
    def __init__(self, path: str | Path = ":memory:"):
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def __enter__(self) -> SnapshotStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    # --- runs & snapshots ---------------------------------------------------
    def create_run(self, project_key: str, *, created_at: datetime | None = None) -> int:
        ts = (created_at or datetime.now(UTC)).isoformat()
        cur = self._conn.execute(
            "INSERT INTO runs (project_key, created_at) VALUES (?, ?)", (project_key, ts)
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def save_snapshot(self, run_id: int, snapshot: ProjectSnapshot) -> None:
        self._conn.executemany(
            "INSERT INTO issue_snapshots (run_id, key, summary, status, status_category, "
            "priority, assignee_id, assignee_name, due, start) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, i.key, i.summary, i.status, i.status_category,
                    i.priority, i.assignee_id, i.assignee_name, _iso(i.due), _iso(i.start),
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

    def previous_run_id(self, project_key: str, before_run: int) -> int | None:
        """The most recent run for the project strictly before `before_run`."""
        row = self._conn.execute(
            "SELECT run_id FROM runs WHERE project_key = ? AND run_id < ? "
            "ORDER BY run_id DESC LIMIT 1",
            (project_key, before_run),
        ).fetchone()
        return int(row["run_id"]) if row else None

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
