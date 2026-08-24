"""Where the readings land: Postgres on reid-eval-store (RC1-305).

The store was decided in RC1-304 and the reasoning lives in
`docs/kpi/metrics-store.md`: Datadog's free plan forgets a metric within a
day, and this epic needs a weekly KPI read back over ten-plus weeks. The
eval-run store's Postgres already keeps history for nothing and Grafana
reads it directly over the built-in SQL data source, so the readings go
there and the dashboard is a query rather than an export.

The snapshots stay in SQLite (`store/snapshot_store.py`). A reading carries
the `run_id` it was computed from so every number traces back to the day
someone looked — `python -m collectors show <program> --run N` prints that
snapshot. It is a reference, not a foreign key: the two live in different
databases, and the id is only meaningful against the machine's `drift.db`.
That holds while the launchd job is the single writer, which it is; a second
writer would need the snapshot store to move here too.

`EVAL_DATABASE_URL` has exactly one home, `~/.zshrc` (RC1-263), and is read
from the process environment — never from the repo `.env`. psycopg2 is
imported lazily, the way `collectors/program.py` does it, so nothing that
merely imports this module needs a Postgres driver.

The rubric's honesty rule is a table constraint here, not only a validator
in `kpi/reading.py`: a row whose state is not `ok` must carry a reason. A
KPI that cannot be measured is written `stale` or `broken`, never `0`, and
the database refuses the alternative.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from kpi.reading import Reading

TABLE = "kpi_readings"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    program_id  text        NOT NULL,
    kpi_id      text        NOT NULL,
    sim_date    date        NOT NULL,
    value       double precision,
    state       text        NOT NULL,
    tripped     boolean     NOT NULL DEFAULT false,
    as_of       date,
    reason      text,
    detail      text        NOT NULL DEFAULT '',
    run_id      integer     NOT NULL,
    computed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (program_id, kpi_id, sim_date),
    CONSTRAINT kpi_readings_state_known
        CHECK (state IN ('ok', 'stale', 'broken')),
    CONSTRAINT kpi_readings_not_ok_needs_a_reason
        CHECK (state = 'ok' OR reason IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS kpi_readings_program_day
    ON {TABLE} (program_id, sim_date);
"""

#: A day can be tracked more than once — a re-run, or a recompute after a fix.
#: The last word wins, matching `SnapshotStore.latest_program_run(sim_date=...)`.
UPSERT = f"""
INSERT INTO {TABLE}
    (program_id, kpi_id, sim_date, value, state, tripped, as_of, reason, detail, run_id,
     computed_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (program_id, kpi_id, sim_date) DO UPDATE SET
    value       = EXCLUDED.value,
    state       = EXCLUDED.state,
    tripped     = EXCLUDED.tripped,
    as_of       = EXCLUDED.as_of,
    reason      = EXCLUDED.reason,
    detail      = EXCLUDED.detail,
    run_id      = EXCLUDED.run_id,
    computed_at = EXCLUDED.computed_at
"""

_COLUMNS = (
    "program_id, kpi_id, sim_date, value, state, tripped, as_of, reason, detail, run_id, "
    "computed_at"
)


@dataclass(frozen=True)
class StoredReading:
    """A reading as it came back out, with what the row adds to it."""

    program_id: str
    run_id: int
    computed_at: datetime
    reading: Reading


def row_for(reading: Reading, *, program_id: str, run_id: int) -> tuple:
    """The UPSERT's parameters for one reading. Pure — the tests read this
    rather than a database."""
    return (
        program_id,
        reading.kpi_id,
        reading.sim_date,
        reading.value,
        reading.state,
        reading.tripped,
        reading.as_of,
        reading.reason,
        reading.detail,
        run_id,
    )


def stored_from_row(row: Any) -> StoredReading:
    """The inverse of `row_for`, over a SELECT of `_COLUMNS`. Pure."""
    (
        program_id, kpi_id, sim_date, value, state, tripped, as_of, reason, detail, run_id,
        computed_at,
    ) = row
    return StoredReading(
        program_id=program_id,
        run_id=int(run_id),
        computed_at=computed_at,
        reading=Reading(
            kpi_id=kpi_id,
            sim_date=sim_date,
            value=None if value is None else float(value),
            state=state,
            tripped=bool(tripped),
            as_of=as_of,
            reason=reason,
            detail=detail or "",
        ),
    )


class ReadingsStore:
    """The readings table, opened on a DSN.

    Used as a context manager so the connection closes on the way out:

        with ReadingsStore(dsn) as store:
            store.save("simulated-program", readings, run_id=7)
    """

    def __init__(self, dsn: str, *, sslmode: str = "require"):
        import psycopg2  # lazy: only this store needs a Postgres driver

        if not dsn:
            raise ValueError("no DSN: EVAL_DATABASE_URL is not set (it lives in ~/.zshrc)")
        self._conn = psycopg2.connect(dsn, sslmode=sslmode)
        self.ensure_schema()

    def __enter__(self) -> ReadingsStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def ensure_schema(self) -> None:
        with self._conn, self._conn.cursor() as cur:
            cur.execute(SCHEMA)

    def save(self, program_id: str, readings: list[Reading], *, run_id: int) -> int:
        """Write every reading for one program's day. Returns the row count.

        One transaction: a day's readings land together or not at all, so a
        brief never reads half a day.
        """
        rows = [row_for(r, program_id=program_id, run_id=run_id) for r in readings]
        if not rows:
            return 0
        with self._conn, self._conn.cursor() as cur:
            cur.executemany(UPSERT, rows)
        return len(rows)

    def readings(
        self,
        program_id: str,
        *,
        kpi_id: str | None = None,
        since: date | None = None,
    ) -> list[StoredReading]:
        """A program's readings, oldest first — what the dashboard charts and
        what RC1-306 will narrate from."""
        query = f"SELECT {_COLUMNS} FROM {TABLE} WHERE program_id = %s"
        params: list[Any] = [program_id]
        if kpi_id is not None:
            query += " AND kpi_id = %s"
            params.append(kpi_id)
        if since is not None:
            query += " AND sim_date >= %s"
            params.append(since)
        query += " ORDER BY sim_date, kpi_id"
        with self._conn.cursor() as cur:
            cur.execute(query, params)
            return [stored_from_row(row) for row in cur.fetchall()]

    def days(self, program_id: str) -> list[date]:
        """Every sim-date the program has readings for, oldest first. The
        'landing on schedule for a week' check reads this."""
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT sim_date FROM {TABLE} WHERE program_id = %s ORDER BY sim_date",
                (program_id,),
            )
            return [r[0] for r in cur.fetchall()]
