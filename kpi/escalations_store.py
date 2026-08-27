"""Where escalations are recorded: beside the readings (RC1-307).

Same Postgres, same reasoning as `kpi/readings_store.py` and
`kpi/briefs_store.py`: the readings an escalation is about live in
`kpi_readings`, the brief that must carry it reads this table, and history
is the point — "how long was this source down" is a query, not a memory.

One row per (program, day, kind, subject). `subject` is the thing that
went wrong: the source name for a `source` escalation, the KPI id for the
per-KPI kinds. `kpi_ids` is the blast radius — every shipping KPI the
problem makes unmeasurable or suspect. `healed` rows are kept: a retry
that fixed the source on the spot is still a fact about the day, and the
brief reports it as recovery rather than silence.

`posted_at` is null until Slack actually accepted the post, exactly as the
briefs archive does it. psycopg2 is imported lazily and `EVAL_DATABASE_URL`
stays in `~/.zshrc` (RC1-263).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

TABLE = "kpi_escalations"

KINDS = ("source", "reading", "flatline", "implausible")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    program_id   text        NOT NULL,
    sim_date     date        NOT NULL,
    kind         text        NOT NULL,
    subject      text        NOT NULL,
    kpi_ids      jsonb       NOT NULL,
    reason       text        NOT NULL,
    proposed_fix text        NOT NULL,
    healed       boolean     NOT NULL DEFAULT false,
    run_id       integer     NOT NULL,
    posted_at    timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (program_id, sim_date, kind, subject),
    CONSTRAINT kpi_escalations_kind_known
        CHECK (kind IN ('source', 'reading', 'flatline', 'implausible')),
    CONSTRAINT kpi_escalations_reason_not_empty CHECK (reason <> ''),
    CONSTRAINT kpi_escalations_fix_not_empty CHECK (proposed_fix <> '')
);
CREATE INDEX IF NOT EXISTS kpi_escalations_program_day
    ON {TABLE} (program_id, sim_date);
"""

#: A day can be escalated more than once (a re-run after a fix). The last
#: word wins; a `posted_at` already set is kept — the record of what was
#: sent survives the recompute.
UPSERT = f"""
INSERT INTO {TABLE}
    (program_id, sim_date, kind, subject, kpi_ids, reason, proposed_fix, healed, run_id,
     posted_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (program_id, sim_date, kind, subject) DO UPDATE SET
    kpi_ids      = EXCLUDED.kpi_ids,
    reason       = EXCLUDED.reason,
    proposed_fix = EXCLUDED.proposed_fix,
    healed       = EXCLUDED.healed,
    run_id       = EXCLUDED.run_id,
    posted_at    = COALESCE({TABLE}.posted_at, EXCLUDED.posted_at)
"""

_COLUMNS = (
    "program_id, sim_date, kind, subject, kpi_ids, reason, proposed_fix, healed, run_id, "
    "posted_at, created_at"
)


@dataclass(frozen=True)
class Escalation:
    """One thing the escalate stage decided a human should hear about."""

    program_id: str
    sim_date: date
    kind: str  # source | reading | flatline | implausible
    subject: str  # the source name, or the KPI id for the per-KPI kinds
    kpi_ids: tuple[str, ...]  # blast radius: shipping KPIs affected
    reason: str
    proposed_fix: str
    run_id: int
    healed: bool = False
    posted_at: datetime | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown escalation kind {self.kind!r}")
        if not self.reason or not self.proposed_fix:
            raise ValueError(f"{self.kind}:{self.subject}: reason and proposed_fix are required")


def row_for(esc: Escalation) -> tuple:
    """The UPSERT's parameters. Pure — the tests read this, not a database."""
    return (
        esc.program_id,
        esc.sim_date,
        esc.kind,
        esc.subject,
        json.dumps(list(esc.kpi_ids)),
        esc.reason,
        esc.proposed_fix,
        esc.healed,
        esc.run_id,
        esc.posted_at,
    )


def escalation_from_row(row: Any) -> Escalation:
    """The inverse of `row_for`, over a SELECT of `_COLUMNS`. Pure."""
    (
        program_id, sim_date, kind, subject, kpi_ids, reason, proposed_fix, healed, run_id,
        posted_at, created_at,
    ) = row
    return Escalation(
        program_id=program_id,
        sim_date=sim_date,
        kind=kind,
        subject=subject,
        kpi_ids=tuple(kpi_ids if isinstance(kpi_ids, list) else json.loads(kpi_ids)),
        reason=reason,
        proposed_fix=proposed_fix,
        healed=bool(healed),
        run_id=int(run_id),
        posted_at=posted_at,
        created_at=created_at,
    )


class EscalationsStore:
    """The escalations table, opened on a DSN. Context manager, like the others."""

    def __init__(self, dsn: str, *, sslmode: str = "require"):
        import psycopg2  # lazy: only this store needs a Postgres driver

        if not dsn:
            raise ValueError("no DSN: EVAL_DATABASE_URL is not set (it lives in ~/.zshrc)")
        self._conn = psycopg2.connect(dsn, sslmode=sslmode)
        self.ensure_schema()

    def __enter__(self) -> EscalationsStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def ensure_schema(self) -> None:
        with self._conn, self._conn.cursor() as cur:
            cur.execute(SCHEMA)

    def save(self, escalations: list[Escalation]) -> int:
        """Write a day's escalations. One transaction, like the readings."""
        if not escalations:
            return 0
        with self._conn, self._conn.cursor() as cur:
            cur.executemany(UPSERT, [row_for(e) for e in escalations])
        return len(escalations)

    def escalations(
        self, program_id: str, *, since: date | None = None
    ) -> list[Escalation]:
        """A program's escalations, oldest first — what the brief reads."""
        query = f"SELECT {_COLUMNS} FROM {TABLE} WHERE program_id = %s"
        params: list[Any] = [program_id]
        if since is not None:
            query += " AND sim_date >= %s"
            params.append(since)
        query += " ORDER BY sim_date, kind, subject"
        with self._conn.cursor() as cur:
            cur.execute(query, params)
            return [escalation_from_row(row) for row in cur.fetchall()]

    def already_raised(
        self, esc: Escalation, *, within_days: int = 7
    ) -> bool:
        """True when the same (kind, subject) was escalated un-healed within
        the window — the Slack dedup: a break that persists re-posts weekly,
        not every morning."""
        floor = esc.sim_date - timedelta(days=within_days)
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT 1 FROM {TABLE} WHERE program_id = %s AND kind = %s AND subject = %s "
                "AND sim_date >= %s AND sim_date < %s AND NOT healed AND posted_at IS NOT NULL "
                "LIMIT 1",
                (esc.program_id, esc.kind, esc.subject, floor, esc.sim_date),
            )
            return cur.fetchone() is not None

    def mark_posted(self, escalations: list[Escalation]) -> datetime:
        """Stamp the given rows as posted, now. Returns the stamp it wrote."""
        when = datetime.now(UTC)
        with self._conn, self._conn.cursor() as cur:
            for e in escalations:
                cur.execute(
                    f"UPDATE {TABLE} SET posted_at = %s WHERE program_id = %s "
                    "AND sim_date = %s AND kind = %s AND subject = %s",
                    (when, e.program_id, e.sim_date, e.kind, e.subject),
                )
        return when


def with_healed(esc: Escalation, *, fix_applied: str) -> Escalation:
    """The escalation as recorded after a retry fixed it on the spot."""
    return replace(esc, healed=True, proposed_fix=fix_applied)
