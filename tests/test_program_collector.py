"""Program snapshots — offline (RC1-301).

The done-when: a day's KPI values can be recomputed from its snapshot alone.
Checked end to end without a network: the simulator converges the in-memory
Jira from `test_simulate`, the collector parses it with the same functions
that parse live Jira, the store round-trips it, the ledger adapter turns it
into the ledger's shape, and the ledger formulas over the *collected* series
must equal the ledger derived from the scenario — reading for reading.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from collectors import program as collect
from collectors import programs
from collectors.jira import ISSUE_FIELDS, POINTS_FIELD, parse_issue, snapshot_from_raw
from collectors.models import ProgramSnapshot, SourceHealth, SpendRow
from collectors.programs import JiraSource, Program
from simulate import apply, ledger, scenario
from simulate.clock import SimState
from store.snapshot_store import SnapshotStore
from tests.test_simulate import FakeJira, _quiet

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


class FakeCollector:
    """`JiraCollector.collect` over the simulator's in-memory Jira: the search
    result is the fake's issues, parsed by the real parser."""

    def __init__(self, jira: FakeJira) -> None:
        self.jira = jira
        self.calls: list[str] = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def collect(self, project_key, *, jql=None, with_changelog=True):
        assert not with_changelog, "program snapshots must not read the changelog"
        self.calls.append(jql)
        raw = self.jira.search(jql, ISSUE_FIELDS)
        return snapshot_from_raw(project_key, raw)


def _program(tmp_path: Path, **over) -> Program:
    base = dict(
        id="simulated-program",
        name="sim",
        jira=JiraSource("PMA", 'project = PMA AND labels = "kpi-sim"'),
        spend_csv=str(tmp_path / "sim" / "spend.csv"),
        clock_dir=str(tmp_path / "sim"),
    )
    base.update(over)
    return Program(**base)


def _converge(jira: FakeJira, state: SimState, day: int) -> None:
    report = apply.converge(jira, day, board_id=68, log=_quiet)
    state.write(day, report.keys)


# --- parsing the new fields --------------------------------------------------------------


def test_parser_reads_labels_points_type_created_and_parent():
    raw = {
        "key": "PMA-9",
        "fields": {
            "summary": "s", "status": {"name": "Done", "statusCategory": {"name": "Done"}},
            "issuetype": {"name": "Story"}, "labels": ["kpi-sim", "ks-t-sdk"],
            POINTS_FIELD: 5.0, "created": "2026-09-07T09:00:00.000+0000",
            "parent": {"key": "PMA-1"}, "duedate": "2026-09-13",
        },
    }
    issue = parse_issue(raw)
    assert issue.issue_type == "Story" and issue.labels == ["kpi-sim", "ks-t-sdk"]
    assert issue.points == 5.0 and issue.created == date(2026, 9, 7) and issue.parent == "PMA-1"
    bare = parse_issue({"key": "X-1", "fields": {}})
    assert bare.labels == [] and bare.points is None and bare.issue_type is None


def test_seed_and_collector_agree_on_field_ids():
    from seed import jira_client

    assert jira_client.POINTS_FIELD == POINTS_FIELD
    assert jira_client.START_DATE_FIELD in ISSUE_FIELDS and POINTS_FIELD in ISSUE_FIELDS


# --- health: missing, never zero ---------------------------------------------------------


def test_every_source_is_reported_and_a_dead_jira_is_absent_not_empty(tmp_path):
    prog = _program(tmp_path, eval_store=True)
    snap = collect.collect_program(prog, jira=None, eval_dsn=None, now=NOW)
    assert {h.source: h.status for h in snap.health} == {
        "clock": "missing", "jira": "error", "spend": "missing", "eval-store": "error",
    }
    assert snap.jira is None and snap.spend == [] and snap.eval_runs == []
    assert snap.sim_day is None and snap.sim_date == NOW.date()  # wall-clock fallback
    assert not snap.healthy
    assert "not seeded" in snap.source("clock").detail
    assert "EVAL_DATABASE_URL" in snap.source("eval-store").detail


def test_an_empty_jira_answer_is_missing_and_a_failing_one_is_an_error(tmp_path):
    from collectors.jira import JiraError

    class Empty:
        def collect(self, *a, **k):
            return snapshot_from_raw("PMA", [])

    class Broken:
        def collect(self, *a, **k):
            raise JiraError("HTTP 503")

    prog = _program(tmp_path)
    missing = collect.collect_program(prog, jira=Empty(), now=NOW)
    assert missing.source("jira").status == "missing" and missing.jira is not None
    assert missing.jira.issues == []
    broken = collect.collect_program(prog, jira=Broken(), now=NOW)
    assert broken.source("jira").status == "error" and broken.jira is None
    assert "HTTP 503" in broken.source("jira").detail


def test_snapshot_carries_the_sim_clock_and_the_landed_spend(tmp_path):
    jira, state = FakeJira(), SimState(tmp_path / "sim")
    _converge(jira, state, 42)
    snap = collect.collect_program(_program(tmp_path), jira=FakeCollector(jira), now=NOW)
    assert snap.healthy
    assert (snap.sim_day, snap.sim_date) == (42, scenario.sim_date(42))
    assert snap.collected_at == NOW
    assert snap.source("jira").count == 35  # 34 stories + the epic carry the label on day 42
    assert [r.week for r in snap.spend] == [1, 2, 3, 4, 5, 6]
    assert snap.spend[-1].actual_usd == 2450.0 and snap.spend[-1].landed_on_day == 42
    epic = next(i for i in snap.jira.issues if i.issue_type == "Epic")
    assert epic.due == scenario.sim_date(scenario.GA_DAY)


def test_the_source_break_is_collected_as_missing(tmp_path):
    jira, state = FakeJira(), SimState(tmp_path / "sim")
    _converge(jira, state, 45)
    snap = collect.collect_program(_program(tmp_path), jira=FakeCollector(jira), now=NOW)
    assert snap.source("jira").status == "missing" and snap.source("jira").count == 0
    assert snap.jira is not None and snap.jira.issues == []
    assert snap.source("spend").status == "ok"  # a different source keeps reporting


# --- eval-store rows ----------------------------------------------------------------------


def test_eval_run_row_counts_gating_results_only():
    record = {
        "run_id": "kpi-ledger-20260822T230240Z",
        "subject_version": {"subject": "kpi-ledger", "code_version": "0.1.0", "model": None},
        "started_at": "2026-08-22T23:02:40+00:00",
        "results": [
            {"characteristics": [{"passed": True, "advisory": False}],
             "usage": {"cost_usd": "0.1"}},
            {"characteristics": [{"passed": False, "advisory": True}],
             "usage": {"cost_usd": "0"}},
            {"characteristics": [{"passed": False, "advisory": False}], "usage": {}},
            {"error": "boom", "characteristics": [], "usage": {"cost_usd": "0.05"}},
        ],
    }
    row = collect.eval_run_row(record)
    assert (row.cases, row.passed, row.errored) == (4, 2, 1)
    assert row.cost_usd == pytest.approx(0.15) and row.model is None


# --- the store ----------------------------------------------------------------------------------


def test_program_snapshot_round_trips_through_the_store(tmp_path):
    jira, state = FakeJira(), SimState(tmp_path / "sim")
    _converge(jira, state, 30)
    snap = collect.collect_program(_program(tmp_path), jira=FakeCollector(jira), now=NOW)
    with SnapshotStore(":memory:") as store:
        run_id = store.save_program_snapshot(snap, project_key="PMA")
        again = store.load_program_snapshot(run_id)
        latest = store.latest_program_run
        assert latest("simulated-program").run_id == run_id
        assert latest("simulated-program", sim_date=scenario.sim_date(30)).run_id == run_id
        assert latest("simulated-program", sim_date=scenario.sim_date(31)) is None
    # The store reloads issues and links sorted by key; the content is identical.
    def canon(ps: ProgramSnapshot) -> dict:
        d = ps.model_dump()
        d["jira"]["issues"].sort(key=lambda i: i["key"])
        d["jira"]["links"].sort(key=lambda lk: (lk["upstream"], lk["downstream"]))
        return d

    assert canon(again) == canon(snap)


def test_an_errored_jira_source_reloads_as_absent_not_empty():
    snap = ProgramSnapshot(
        program_id="p", collected_at=NOW, sim_date=NOW.date(),
        health=[SourceHealth(source="jira", status="error", detail="down")],
        spend=[SpendRow(week=1, week_start=date(2026, 9, 7), planned_usd=1, actual_usd=2)],
    )
    with SnapshotStore(":memory:") as store:
        run_id = store.save_program_snapshot(snap, project_key="PMA")
        loaded = store.load_program_snapshot(run_id)
        assert loaded.jira is None and loaded.spend == snap.spend
        with pytest.raises(KeyError):
            store.load_program_snapshot(run_id + 1)
        drift_run = store.create_run("RC1")  # a drift run is not a program snapshot
        with pytest.raises(KeyError):
            store.load_program_snapshot(drift_run)


def test_an_existing_drift_database_is_migrated_in_place(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE runs (run_id INTEGER PRIMARY KEY AUTOINCREMENT, project_key TEXT NOT NULL, "
        "created_at TEXT NOT NULL);"
        "CREATE TABLE issue_snapshots (run_id INTEGER NOT NULL, key TEXT NOT NULL, "
        "summary TEXT NOT NULL, status TEXT NOT NULL, status_category TEXT NOT NULL, "
        "priority TEXT, assignee_id TEXT, assignee_name TEXT, due TEXT, start TEXT);"
        "INSERT INTO runs (project_key, created_at) VALUES ('RC1', '2026-08-01T00:00:00+00:00');"
        "INSERT INTO issue_snapshots VALUES (1, 'RC1-1', 's', 'Done', 'Done', NULL, NULL, NULL, "
        "NULL, NULL);"
    )
    conn.commit()
    conn.close()
    with SnapshotStore(path) as store:
        old = store.load_previous("RC1")
        assert old is not None and old.issues[0].labels == [] and old.issues[0].points is None
        assert store.program_runs("simulated-program") == []
        cols = {r[1] for r in store._conn.execute("PRAGMA table_info(runs)")}
        assert {"program_id", "sim_date", "sim_day"} <= cols
    with SnapshotStore(path) as store:  # idempotent on the second open
        assert store.latest_run("RC1").run_id == 1


# --- the done-when ------------------------------------------------------------------------------


def test_a_days_kpis_recompute_from_collected_snapshots_alone(tmp_path):
    """Simulator -> collector -> store -> ledger adapter -> the same readings."""
    jira, state = FakeJira(), SimState(tmp_path / "sim")
    prog = _program(tmp_path)
    collector = FakeCollector(jira)
    truth = ledger.derive()
    series: list[ledger.Snapshot] = []
    with SnapshotStore(tmp_path / "snapshots.db") as store:
        for day in range(scenario.LAST_DAY + 1):
            _converge(jira, state, day)
            snap = collect.collect_program(prog, jira=collector, now=NOW)
            store.save_program_snapshot(snap, project_key="PMA")
        # Recompute offline, from the store, nothing from Jira or the scenario's state.
        for run in store.program_runs("simulated-program"):
            series.append(ledger.snapshot_from_collected(store.load_program_snapshot(run.run_id)))
    assert all(jql == prog.jira.jql for jql in collector.calls)
    recomputed = ledger.derive(series=series)
    assert recomputed.days == truth.days
    mismatches = [
        (r.day, r.kpi_id, r.expected, truth.reading(r.day, r.kpi_id))
        for r in recomputed.rows
        if r.expected != truth.reading(r.day, r.kpi_id)
    ]
    assert mismatches == []


def test_the_adapter_needs_a_sim_day_and_a_contiguous_series():
    snap = ProgramSnapshot(program_id="p", collected_at=NOW, sim_date=NOW.date())
    with pytest.raises(ValueError, match="sim_day"):
        ledger.snapshot_from_collected(snap)
    with pytest.raises(ValueError, match="contiguous|from day 0"):
        ledger.derive(series=[ledger.snapshot(0), ledger.snapshot(2)])


# --- the CLI --------------------------------------------------------------------------------------


def test_cli_snapshot_runs_and_show(tmp_path, monkeypatch, capsys):
    from collectors import __main__ as cli

    jira, state = FakeJira(), SimState(tmp_path / "sim")
    _converge(jira, state, 16)
    prog = _program(tmp_path)
    monkeypatch.setattr(cli, "_jira", lambda: FakeCollector(jira))
    monkeypatch.setitem(programs.PROGRAMS, "simulated-program", prog)
    monkeypatch.delenv("EVAL_DATABASE_URL", raising=False)
    db = str(tmp_path / "snap.db")

    assert cli.main(["--db", db, "snapshot", "simulated-program"]) == 0
    out = capsys.readouterr().out
    assert "sim-day 16" in out and "stored as run 1" in out and "jira       ok" in out

    _converge(jira, state, 45)  # the break
    assert cli.main(["--db", db, "snapshot", "simulated-program"]) == 1
    assert "? jira       missing" in capsys.readouterr().out

    assert cli.main(["--db", db, "runs", "simulated-program"]) == 0
    out = capsys.readouterr().out
    assert "jira=ok(34)" in out and "jira=missing" in out

    assert cli.main(["--db", db, "show", "simulated-program", "--sim-date",
                     scenario.sim_date(16).isoformat()]) == 0
    assert "34 issue(s)" in capsys.readouterr().out
    assert cli.main(["--db", db, "show", "simulated-program", "--run", "2", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["sim_day"] == 45
    assert cli.main(["--db", db, "show", "simulated-program", "--sim-date", "2030-01-01"]) == 1


def test_registry_names_the_two_programs_with_briefs():
    briefs = {p.stem for p in (Path(__file__).parent.parent / "docs/kpi/programs").glob("*.md")}
    assert set(programs.PROGRAMS) == briefs
    assert programs.get("simulated-program").project_key == "PMA"
    assert programs.get("eval-run-store").project_key == "eval-run-store"
    with pytest.raises(KeyError, match="registered"):
        programs.get("nope")
