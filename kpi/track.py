"""Track: compute every shipping KPI from the stored snapshots (RC1-305).

The fourth stage. Define drafted the tree, instrument proved which KPIs
actually compute, and this runs them on a schedule and lands the numbers
where a dashboard can read them — Postgres on reid-eval-store, decided in
RC1-304 (`docs/kpi/metrics-store.md`).

Nothing here calls a model. The measures in `kpi/measures.py` are ordinary
Python over the snapshot series, and that is the epic's "deterministic
numbers, LLM narrative" line drawn in code: RC1-306 will write prose *about*
these readings and will not be allowed to compute one.

What ships is the instrument stage's verdict, not this stage's opinion:
`docs/kpi/instruments/<program>.json` lists every KPI it verified, and only
those are tracked. A rejected KPI has no source, a proxied-but-unverified
one has nothing that computes it, and neither gets to appear on a dashboard
looking like a number somebody stands behind.

Three rules the code enforces rather than promises:

- **A measure that raises is a broken reading, not a lost day.** One KPI
  blowing up must not take the other five down with it; the exception
  becomes the reason on a `broken` reading and the run carries on.
- **Never a zero for unknown.** Every path out of here is a `Reading`, whose
  own validator refuses a non-ok state without a reason, and whose table
  refuses it again on the way into Postgres.
- **A tracked day names the snapshot it came from.** The run id rides along
  on every row so RC1-306's "trace every number back to its snapshot" is a
  lookup, not an archaeology exercise.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from collectors.models import ProgramSnapshot
from collectors.programs import Program
from config import settings
from kpi import measures
from kpi.instrument import INSTRUMENTS, Instrumentation
from kpi.reading import Reading
from store.snapshot_store import SnapshotStore


@dataclass(frozen=True)
class TrackResult:
    """One program, one day, every shipping KPI read."""

    program_id: str
    run_id: int
    sim_date: date
    readings: list[Reading]

    def by_state(self, state: str) -> list[Reading]:
        return [r for r in self.readings if r.state == state]

    @property
    def ok(self) -> list[Reading]:
        return self.by_state("ok")

    @property
    def unmeasured(self) -> list[Reading]:
        """Stale or broken — the readings the escalate stage (RC1-307) owns."""
        return [r for r in self.readings if r.state != "ok"]

    @property
    def tripped(self) -> list[Reading]:
        return [r for r in self.readings if r.tripped]


def load_instrumentation(
    program_id: str, *, instruments_dir: Path = INSTRUMENTS
) -> Instrumentation:
    """The instrument stage's verdicts for a program. Raises when the program
    has not been instrumented — tracking an uninstrumented tree would be
    shipping numbers nobody verified a source for."""
    path = instruments_dir / f"{program_id}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist: run `python -m kpi.instrument --program {program_id}` first"
        )
    return Instrumentation.model_validate_json(path.read_text())


def track(
    program: Program,
    series: list[ProgramSnapshot],
    shipping: list[str],
) -> list[Reading]:
    """Run every shipping KPI's measure over the series, newest snapshot last.

    A measure that raises yields a `broken` reading carrying the exception,
    because a stage that dies on one bad KPI reports nothing at all — and
    nothing at all is indistinguishable from a program with no problems.
    """
    if not series:
        raise ValueError(f"{program.id}: no snapshots stored; nothing to track")
    snap = series[-1]
    readings: list[Reading] = []
    for kpi_id in shipping:
        try:
            readings.append(measures.measure(kpi_id, program, series))
        except KeyError:
            readings.append(
                Reading(
                    kpi_id=kpi_id,
                    sim_date=snap.sim_date,
                    value=None,
                    state="broken",
                    reason=(
                        "no measure is registered: the instrument stage verified this KPI "
                        "but nothing computes it"
                    ),
                )
            )
        except Exception as exc:
            readings.append(
                Reading(
                    kpi_id=kpi_id,
                    sim_date=snap.sim_date,
                    value=None,
                    state="broken",
                    reason=f"measure raised: {type(exc).__name__}: {exc}",
                )
            )
    return readings


def track_program(
    program: Program,
    *,
    db_path: str,
    sim_date: date | None = None,
    instruments_dir: Path = INSTRUMENTS,
) -> TrackResult:
    """Load the program's snapshots and read every shipping KPI for one day.

    `sim_date` tracks a specific day — the latest run *for that day*, since a
    day can be snapshotted more than once and the last word is the one a
    recompute should use. Omitted, it tracks the newest day there is.
    """
    inst = load_instrumentation(program.id, instruments_dir=instruments_dir)
    with SnapshotStore(db_path) as store:
        runs = store.program_runs(program.id)
        if not runs:
            raise ValueError(f"{program.id}: no snapshots stored; nothing to track")
        target = store.latest_program_run(program.id, sim_date=sim_date)
        if target is None:
            raise ValueError(f"{program.id}: no snapshot for sim-date {sim_date}")
        # The series ends at the tracked run: a measure comparing against
        # history must not see days that had not happened yet.
        series = [
            store.load_program_snapshot(r.run_id) for r in runs if r.run_id <= target.run_id
        ]
    readings = track(program, series, inst.computes)
    return TrackResult(
        program_id=program.id,
        run_id=target.run_id,
        sim_date=series[-1].sim_date,
        readings=readings,
    )


# --- CLI -------------------------------------------------------------------------------------


def _print_result(result: TrackResult, *, stored: int | None) -> None:
    print(f"{result.program_id}: {result.sim_date} (run {result.run_id})")
    for r in result.readings:
        mark = {"ok": " ", "stale": "?", "broken": "!"}[r.state]
        value = "—" if r.value is None else f"{r.value:g}"
        trip = " TRIPPED" if r.tripped else ""
        note = f"  {r.reason}" if r.reason else ""
        print(f"  {mark} {r.kpi_id:<28} {r.state:<7} {value:>10}{trip}{note}")
    print(
        f"  {len(result.ok)} ok, {len(result.by_state('stale'))} stale, "
        f"{len(result.by_state('broken'))} broken, {len(result.tripped)} tripped"
    )
    if stored is not None:
        print(f"  stored {stored} reading(s)")


def main(argv: list[str] | None = None) -> int:
    """Exit codes mirror `python -m collectors snapshot`: 0 every KPI read ok;
    1 at least one is stale or broken. The readings are stored either way —
    a day a KPI could not be measured is exactly the day worth recording."""
    from collectors import programs

    ap = argparse.ArgumentParser(
        prog="python -m kpi.track",
        description="Compute every shipping KPI from the stored snapshots and land the readings.",
    )
    ap.add_argument("--program", required=True, choices=sorted(programs.PROGRAMS))
    ap.add_argument("--db", default=settings.db_path, help="snapshot store (default: DB_PATH)")
    ap.add_argument(
        "--sim-date", type=date.fromisoformat, default=None,
        help="track this day rather than the newest (YYYY-MM-DD)",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="compute and print, write nothing — no Postgres connection is made",
    )
    args = ap.parse_args(argv)

    program = programs.get(args.program)
    try:
        result = track_program(program, db_path=args.db, sim_date=args.sim_date)
    except (FileNotFoundError, ValueError) as exc:
        print(f"{args.program}: {exc}", file=sys.stderr)
        return 2

    stored: int | None = None
    if not args.dry_run:
        dsn = os.environ.get("EVAL_DATABASE_URL")
        if not dsn:
            print(
                "EVAL_DATABASE_URL is not set (it lives in ~/.zshrc — RC1-263); "
                "nothing was stored",
                file=sys.stderr,
            )
            _print_result(result, stored=None)
            return 2
        from kpi.readings_store import ReadingsStore

        with ReadingsStore(dsn) as store:
            stored = store.save(result.program_id, result.readings, run_id=result.run_id)

        # The Datadog leg (dual-write, RC1-305 revisited): Postgres is the
        # record, Datadog the picture. A failure here is a missing point on a
        # chart, not a lost day — report it, keep the exit code Postgres's.
        from kpi import datadog

        try:
            shipped = datadog.ship_readings(result.readings, program_id=result.program_id)
        except Exception as exc:
            print(f"datadog: shipping failed, readings are in Postgres: {exc}", file=sys.stderr)
        else:
            if shipped is not None:
                print(f"  shipped {shipped} series to Datadog")

    _print_result(result, stored=stored)
    return 1 if result.unmeasured else 0


if __name__ == "__main__":
    raise SystemExit(main())
