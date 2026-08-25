"""Track stage — offline, no Postgres and no live API calls (RC1-305).

The done-when is "every confirmed KPI landing on schedule", so what is tested
here is the part that can be wrong without anyone noticing: that one broken
measure does not take the day's other readings with it, that an unmeasurable
KPI comes out stale or broken rather than zero, and that a day tracked after
the fact sees only the snapshots that had happened by then.

The Postgres store is exercised through its pure halves — the row a reading
becomes and the reading a row comes back as. The connection itself is the
same lazily-imported psycopg2 the collector uses and is verified by running
it, not by mocking a driver.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from collectors.models import ProgramSnapshot, SourceHealth
from kpi import measures, track
from kpi.reading import Reading
from kpi.readings_store import _COLUMNS, SCHEMA, row_for, stored_from_row
from store.snapshot_store import SnapshotStore
from tests.test_kpi_instrument import EVAL, _eval_snapshot, _run

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


# --- track() ---------------------------------------------------------------------------------


def test_reads_every_shipping_kpi():
    series = [_eval_snapshot([_run("a"), _run("b")])]
    readings = track.track(EVAL, series, ["gated-pass-rate", "error-rate"])
    assert [r.kpi_id for r in readings] == ["gated-pass-rate", "error-rate"]
    assert all(r.state == "ok" for r in readings)


def test_a_measure_that_raises_is_a_broken_reading_not_a_lost_day(monkeypatch):
    """One KPI blowing up must not cost the other five their day."""

    def boom(program, series):
        raise RuntimeError("the source moved")

    monkeypatch.setitem(measures.MEASURES, "explodes", boom)
    series = [_eval_snapshot([_run("a")])]

    readings = track.track(EVAL, series, ["explodes", "error-rate"])

    bad, good = readings
    assert bad.state == "broken" and bad.value is None
    assert "RuntimeError: the source moved" in bad.reason
    assert good.kpi_id == "error-rate" and good.state == "ok"


def test_a_verified_kpi_with_no_registered_measure_is_broken():
    series = [_eval_snapshot([_run("a")])]
    (reading,) = track.track(EVAL, series, ["invented-kpi"])
    assert reading.state == "broken" and reading.value is None
    assert "no measure is registered" in reading.reason


def test_an_unmeasurable_kpi_is_never_zero():
    """The rubric's rule, checked on the path that would break it: every
    source gone, every reading still carries a reason and no number."""
    snap = measures.source_missing(_eval_snapshot([_run("a")]))
    readings = track.track(EVAL, [snap], ["gated-pass-rate", "error-rate"])
    assert readings, "expected readings even with every source gone"
    for r in readings:
        assert r.state in ("stale", "broken")
        assert r.value != 0, f"{r.kpi_id} reported zero for unmeasurable"
        assert r.reason


def test_no_snapshots_refuses_rather_than_inventing_a_day():
    with pytest.raises(ValueError, match="no snapshots stored"):
        track.track(EVAL, [], ["error-rate"])


# --- the shipping set ------------------------------------------------------------------------


def test_load_instrumentation_missing_names_the_command_that_fixes_it(tmp_path):
    with pytest.raises(FileNotFoundError, match="kpi.instrument"):
        track.load_instrumentation("simulated-program", instruments_dir=tmp_path)


@pytest.mark.parametrize("program_id", ["simulated-program", "eval-run-store"])
def test_the_shipped_reports_parse_and_name_a_verified_set(program_id):
    """The reports in docs/ are the track stage's input; a change that broke
    their shape would otherwise only show up at 07:00."""
    inst = track.load_instrumentation(program_id)
    assert inst.computes, f"{program_id} has no verified KPI to track"
    assert all(isinstance(k, str) for k in inst.computes)


def test_only_verified_kpis_ship():
    """A rejected or unverified KPI must not reach a dashboard looking like a
    number somebody stands behind."""
    inst = track.load_instrumentation("eval-run-store")
    shipping = set(inst.computes)
    for k in inst.kpis:
        if k.status != "verified":
            assert k.kpi_id not in shipping


# --- track_program(): the series a day is read against ---------------------------------------


def _sim_snapshot(day: int, sim_date: date) -> ProgramSnapshot:
    return ProgramSnapshot(
        program_id="eval-run-store", collected_at=NOW, sim_date=sim_date, sim_day=day,
        eval_runs=[_run("a")],
        health=[SourceHealth(source="eval-store", status="ok", count=1)],
    )


def test_tracking_an_earlier_day_does_not_see_later_snapshots(tmp_path, monkeypatch):
    db = str(tmp_path / "drift.db")
    with SnapshotStore(db) as store:
        for day, d in enumerate([date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]):
            store.save_program_snapshot(_sim_snapshot(day, d), project_key="eval-run-store")

    seen: dict[str, int] = {}

    def counting(program, series):
        seen["days"] = len(series)
        return Reading(kpi_id="error-rate", sim_date=series[-1].sim_date, value=1.0)

    monkeypatch.setitem(measures.MEASURES, "error-rate", counting)
    monkeypatch.setattr(
        track, "load_instrumentation", lambda *a, **k: _fake_instrumentation(["error-rate"])
    )

    result = track.track_program(EVAL, db_path=db, sim_date=date(2026, 9, 2))

    assert result.sim_date == date(2026, 9, 2)
    assert seen["days"] == 2, "a measure must not see days that had not happened yet"


def test_tracking_defaults_to_the_newest_day(tmp_path, monkeypatch):
    db = str(tmp_path / "drift.db")
    with SnapshotStore(db) as store:
        for day, d in enumerate([date(2026, 9, 1), date(2026, 9, 2)]):
            store.save_program_snapshot(_sim_snapshot(day, d), project_key="eval-run-store")

    monkeypatch.setattr(
        track, "load_instrumentation", lambda *a, **k: _fake_instrumentation(["error-rate"])
    )
    result = track.track_program(EVAL, db_path=db)
    assert result.sim_date == date(2026, 9, 2)


def _fake_instrumentation(computes: list[str]):
    from types import SimpleNamespace

    return SimpleNamespace(computes=computes)


def test_result_buckets_readings_by_state():
    result = track.TrackResult(
        program_id="p", run_id=1, sim_date=date(2026, 9, 1),
        readings=[
            Reading(kpi_id="a", sim_date=date(2026, 9, 1), value=1.0),
            Reading(kpi_id="b", sim_date=date(2026, 9, 1), value=None, state="stale", reason="r"),
            Reading(kpi_id="c", sim_date=date(2026, 9, 1), value=2.0, tripped=True),
        ],
    )
    assert [r.kpi_id for r in result.ok] == ["a", "c"]
    assert [r.kpi_id for r in result.unmeasured] == ["b"]
    assert [r.kpi_id for r in result.tripped] == ["c"]


# --- the readings row ------------------------------------------------------------------------


def test_row_and_reading_round_trip():
    reading = Reading(
        kpi_id="gated-pass-rate", sim_date=date(2026, 9, 8), value=87.5, state="ok",
        tripped=True, as_of=date(2026, 9, 7), detail="9/10 cases",
    )
    row = row_for(reading, program_id="eval-run-store", run_id=12)
    stored = stored_from_row((*row, NOW))

    assert stored.program_id == "eval-run-store"
    assert stored.run_id == 12
    assert stored.reading == reading


def test_a_stale_reading_writes_null_not_zero():
    reading = Reading(
        kpi_id="cost-vs-envelope", sim_date=date(2026, 9, 8), value=None, state="stale",
        reason="no spend row has landed yet",
    )
    row = row_for(reading, program_id="simulated-program", run_id=5)
    assert row[3] is None, "value column must be NULL, never 0, for an unmeasurable KPI"
    assert row[4] == "stale" and row[7]


def test_the_row_matches_the_select_columns():
    """`stored_from_row` unpacks positionally; a column added to one side and
    not the other would only fail in production."""
    reading = Reading(kpi_id="k", sim_date=date(2026, 9, 1), value=1.0)
    row = row_for(reading, program_id="p", run_id=1)
    assert len(row) + 1 == len(_COLUMNS.split(","))


def test_the_schema_keeps_the_honesty_constraints():
    """These are the rubric's staleness rule expressed as table constraints;
    dropping one would let a zero in silently."""
    assert "kpi_readings_not_ok_needs_a_reason" in SCHEMA
    assert "state = 'ok' OR reason IS NOT NULL" in SCHEMA
    assert "CHECK (state IN ('ok', 'stale', 'broken'))" in SCHEMA


# --- packaging: kpi ships, simulate does not (RC1-309, RC1-310) ------------------------------


def test_kpi_imports_nothing_from_simulate():
    """`kpi` is in pyproject's packages and copied into the Fly image;
    `simulate` is deliberately neither. An import of `simulate` anywhere under
    `kpi/` — even a lazy one — is a deploy-time ImportError waiting for its
    first caller, which is exactly how RC1-309 happened."""
    import ast
    from pathlib import Path

    import kpi

    offenders = []
    for path in Path(kpi.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            offenders += [
                f"{path.name}:{node.lineno} imports {name}"
                for name in names
                if name == "simulate" or name.startswith("simulate.")
            ]
    assert offenders == []
