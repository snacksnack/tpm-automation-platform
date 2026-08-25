"""Where the weekly briefs are archived: beside the readings (RC1-306).

Same Postgres, same reasoning as `kpi/readings_store.py` (RC1-304): the
readings the brief narrates live in `kpi_readings`, Grafana reads the
database directly, and history is the whole point. A brief row carries the
`run_id` of the snapshot its numbers were computed from and the exact
payload the model was shown, so "trace every number back to its snapshot"
is two lookups: brief -> payload -> `python -m collectors show <program>
--run N`.

The archive keeps three artifacts per (program, week): the `payload` the
model saw, the `narrative` it returned, and the `brief` text that was (or
would be) posted. `posted_at` is null until Slack actually accepted the
post — an archived-but-unposted brief is a draft, and the schedule's job
is to leave no drafts.

psycopg2 is imported lazily and `EVAL_DATABASE_URL` stays in `~/.zshrc`
(RC1-263), exactly as the readings store does it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any

TABLE = "kpi_briefs"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    program_id     text        NOT NULL,
    week_ending    date        NOT NULL,
    run_id         integer     NOT NULL,
    payload        jsonb       NOT NULL,
    narrative      jsonb       NOT NULL,
    brief          text        NOT NULL,
    model          text        NOT NULL,
    prompt_version integer     NOT NULL,
    posted_at      timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (program_id, week_ending)
);
"""

#: A week can be narrated more than once before it posts (a re-run after a
#: fix). The last draft wins; a `posted_at` already set is kept — the archive
#: records what was sent, and a rewrite after posting is a new send, recorded
#: by `mark_posted`.
UPSERT = f"""
INSERT INTO {TABLE}
    (program_id, week_ending, run_id, payload, narrative, brief, model, prompt_version,
     posted_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (program_id, week_ending) DO UPDATE SET
    run_id         = EXCLUDED.run_id,
    payload        = EXCLUDED.payload,
    narrative      = EXCLUDED.narrative,
    brief          = EXCLUDED.brief,
    model          = EXCLUDED.model,
    prompt_version = EXCLUDED.prompt_version,
    posted_at      = COALESCE({TABLE}.posted_at, EXCLUDED.posted_at)
"""

_COLUMNS = (
    "program_id, week_ending, run_id, payload, narrative, brief, model, prompt_version, "
    "posted_at, created_at"
)


@dataclass(frozen=True)
class Brief:
    """One program's week, narrated: what the model saw, what it said, what
    was posted."""

    program_id: str
    week_ending: date
    run_id: int
    payload: dict
    narrative: dict
    brief: str
    model: str
    prompt_version: int
    posted_at: datetime | None = None
    created_at: datetime | None = None


def row_for(brief: Brief) -> tuple:
    """The UPSERT's parameters. Pure — the tests read this, not a database."""
    return (
        brief.program_id,
        brief.week_ending,
        brief.run_id,
        json.dumps(brief.payload),
        json.dumps(brief.narrative),
        brief.brief,
        brief.model,
        brief.prompt_version,
        brief.posted_at,
    )


def brief_from_row(row: Any) -> Brief:
    """The inverse of `row_for`, over a SELECT of `_COLUMNS`. Pure."""
    (
        program_id, week_ending, run_id, payload, narrative, brief, model, prompt_version,
        posted_at, created_at,
    ) = row
    return Brief(
        program_id=program_id,
        week_ending=week_ending,
        run_id=int(run_id),
        payload=payload if isinstance(payload, dict) else json.loads(payload),
        narrative=narrative if isinstance(narrative, dict) else json.loads(narrative),
        brief=brief,
        model=model,
        prompt_version=int(prompt_version),
        posted_at=posted_at,
        created_at=created_at,
    )


class BriefsStore:
    """The briefs table, opened on a DSN. Context manager, like ReadingsStore."""

    def __init__(self, dsn: str, *, sslmode: str = "require"):
        import psycopg2  # lazy: only this store needs a Postgres driver

        if not dsn:
            raise ValueError("no DSN: EVAL_DATABASE_URL is not set (it lives in ~/.zshrc)")
        self._conn = psycopg2.connect(dsn, sslmode=sslmode)
        self.ensure_schema()

    def __enter__(self) -> BriefsStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def ensure_schema(self) -> None:
        with self._conn, self._conn.cursor() as cur:
            cur.execute(SCHEMA)

    def save(self, brief: Brief) -> None:
        with self._conn, self._conn.cursor() as cur:
            cur.execute(UPSERT, row_for(brief))

    def mark_posted(self, program_id: str, week_ending: date) -> datetime:
        """Stamp the brief as posted, now. Returns the stamp it wrote."""
        when = datetime.now(UTC)
        with self._conn, self._conn.cursor() as cur:
            cur.execute(
                f"UPDATE {TABLE} SET posted_at = %s "
                "WHERE program_id = %s AND week_ending = %s",
                (when, program_id, week_ending),
            )
        return when

    def briefs(self, program_id: str) -> list[Brief]:
        """A program's briefs, oldest first — the done-when reads three in a row."""
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM {TABLE} WHERE program_id = %s ORDER BY week_ending",
                (program_id,),
            )
            return [brief_from_row(row) for row in cur.fetchall()]


def with_posted(brief: Brief, when: datetime) -> Brief:
    return replace(brief, posted_at=when)
