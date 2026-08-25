"""Instrument stage, catalog and measures — offline, no live API calls (RC1-303).

The done-when: at least one KPI proxied and one rejected, each with a caveat a
reviewer can argue with, and the confirmed set computes from snapshots without
manual steps. Here the model is a fake returning canned verdicts; what is
tested is what the code enforces around them, and that every registered
measure computes from stored snapshots and reads broken when its source goes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from collectors import program as collect
from collectors import programs
from collectors.models import EvalRunRow, ProgramSnapshot, SourceHealth
from collectors.programs import JiraSource, Program
from kpi import RUBRIC_VERSION, catalog, define, instrument, measures
from kpi.models import validate_shape
from simulate import apply, ledger
from simulate.clock import SimState
from tests.test_program_collector import FakeCollector
from tests.test_simulate import FakeJira, _quiet

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
TODAY = NOW.date()
DOCS = Path(__file__).resolve().parent.parent / "docs" / "kpi"


# --- fixtures --------------------------------------------------------------------------------


def _run(
    subject, *, days_ago=1, cases=10, passed=9, errored=0, cost=0.1, model="claude-x",
    version="1.0.0",
) -> EvalRunRow:
    return EvalRunRow(
        run_id=f"{subject}-{days_ago}", subject=subject, code_version=version, model=model,
        started_at=datetime.combine(TODAY - timedelta(days=days_ago), datetime.min.time(), UTC),
        cases=cases, passed=passed, errored=errored, cost_usd=cost,
    )


def _eval_snapshot(rows, *, today=TODAY, status="ok") -> ProgramSnapshot:
    return ProgramSnapshot(
        program_id="eval-run-store", collected_at=NOW, sim_date=today, eval_runs=rows,
        health=[SourceHealth(source="eval-store", status=status, count=len(rows))],
    )


EVAL = programs.get("eval-run-store")


def _sim_series(tmp_path, days: int) -> tuple[Program, list[ProgramSnapshot]]:
    jira, state = FakeJira(), SimState(tmp_path / "sim")
    prog = Program(
        id="simulated-program", name="sim",
        jira=JiraSource("PMA", 'project = PMA AND labels = "kpi-sim"'),
        spend_csv=str(tmp_path / "sim" / "spend.csv"), clock_dir=str(tmp_path / "sim"),
    )
    series = []
    for day in range(days + 1):
        report = apply.converge(jira, day, board_id=68, log=_quiet)
        state.write(day, report.keys)
        series.append(collect.collect_program(prog, jira=FakeCollector(jira), now=NOW))
    return prog, series


class _FakeClient:
    def __init__(self, canned: dict):
        self._canned = canned
        self.captured: dict = {}
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.captured = kwargs
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(self._canned))],
            usage=SimpleNamespace(input_tokens=5000, output_tokens=2000),
        )


def _verdict(kpi_id, verdict="confirmed", fields=(), **over) -> dict:
    base = dict(kpi_id=kpi_id, verdict=verdict, fields=list(fields), query="q", proxy=None,
                misses=None, reason="", caveat=None)
    base.update(over)
    return base


# --- the adopted trees and the rubric --------------------------------------------------------


def test_adopted_trees_exist_for_every_program_and_pass_the_shape_rules():
    for pid in programs.PROGRAMS:
        tree = instrument.load_adopted_tree(pid)
        validate_shape(tree)
        assert {k.id for k in tree.kpis} == {
            k.id for k in tree.kpis
        }, pid
    sim = instrument.load_adopted_tree("simulated-program")
    assert {k.id for k in sim.kpis} == set(ledger.KPI_IDS)
    evl = instrument.load_adopted_tree("eval-run-store")
    assert "unmeasured-code-versions" in {k.id for k in evl.leading}
    with pytest.raises(instrument.InstrumentError):
        instrument.load_adopted_tree("nope")


def test_rubric_is_v2_and_code_agrees():
    assert RUBRIC_VERSION == 2
    assert define.rubric_version_declared((DOCS / "rubric.md").read_text()) == 2
    assert instrument.prompt_version(instrument.load_prompt()) >= 1


# --- the catalog ------------------------------------------------------------------------------


def test_catalog_lists_only_the_programs_sources_and_says_what_is_missing():
    sim = catalog.catalog(programs.get("simulated-program"))
    assert [s["name"] for s in sim["sources"]] == ["clock", "jira", "spend"]
    assert "jira.points" in sim["field_names"] and "eval-store.passed" not in sim["field_names"]
    assert any("changelog" in n for n in sim["not_available"]["jira"])
    evl = catalog.catalog(EVAL, _eval_snapshot([_run("a")]))
    assert [s["name"] for s in evl["sources"]] == ["clock", "eval-store"]
    assert "constants.store_plan_usd_per_month" in evl["field_names"]
    assert any("advisory" in n for n in evl["not_available"]["eval-store"])
    assert any("GitHub" in n for n in evl["not_available"]["other"])
    assert evl["sample"]["counts"]["eval_runs"] == 1
    assert evl["sample"]["health"][0]["status"] == "ok"


# --- eval-run-store measures ------------------------------------------------------------------


def test_gated_pass_rate_is_the_minimum_over_billed_subjects_case_weighted():
    snap = _eval_snapshot([
        _run("good", passed=10), _run("bad", passed=6),
        _run("free", passed=1, cases=2, model=None),
        _run("old-bad", days_ago=10, passed=2),  # superseded by a later run of the same subject
        _run("old-bad", days_ago=2, passed=9),
    ])
    r = measures.gated_pass_rate(EVAL, [snap])
    assert r.value == 60.0 and r.state == "ok" and not r.tripped and "minimum is bad" in r.detail
    assert r.as_of == TODAY - timedelta(days=1)
    # two consecutive measurements under the floor trip it
    assert measures.gated_pass_rate(EVAL, [snap, snap]).tripped


def test_gated_pass_rate_no_signal_and_stale():
    r = measures.gated_pass_rate(EVAL, [_eval_snapshot([_run("x", cases=3, errored=3, passed=0)])])
    assert r.state == "broken" and r.value is None and "no-signal" in r.reason
    r = measures.gated_pass_rate(EVAL, [_eval_snapshot([_run("x", days_ago=9)])])
    assert r.state == "stale" and r.value == 90.0 and "9 days old" in r.reason


def test_cost_per_verified_case_includes_the_declared_store_cost_and_flags_free_billed_runs():
    snap = _eval_snapshot([_run("a", cost=1.0, cases=10), _run("b", cost=0.0, cases=10)])
    r = measures.cost_per_verified_case(EVAL, [snap])
    fixed = 5.0 * 28 / 30
    assert r.value == pytest.approx((1.0 + fixed) / 20, abs=1e-4)
    assert "BROKEN INSTRUMENT" in r.detail and "b" in r.detail
    assert "$ per full sweep 1.00" in r.detail
    no_plan = Program(id="p", name="p", eval_store=True)
    assert measures.cost_per_verified_case(no_plan, [snap]).state == "broken"
    outside = _eval_snapshot([_run("a", days_ago=40)])
    r = measures.cost_per_verified_case(EVAL, [outside])
    assert r.state == "broken" and "no scored cases" in r.reason


def test_freshness_error_rate_and_cost_by_model():
    snap = _eval_snapshot([
        _run("fresh", days_ago=1), _run("stale", days_ago=12),
        _run("errors", cases=10, errored=4, passed=3),
        _run("cheap", cost=0.05, model="haiku"),
        _run("cheap", cost=0.20, model="sonnet", days_ago=2),
    ])
    f = measures.measurement_freshness_days(EVAL, [snap])
    assert f.value == 12.0 and f.tripped and "oldest is stale" in f.detail
    e = measures.error_rate(EVAL, [snap])
    assert e.value == 40.0 and e.tripped and "worst is errors" in e.detail
    c = measures.cost_per_run_by_model(EVAL, [snap])
    assert c.value == 0.2 and c.tripped and "cheap on sonnet (4.0x)" in c.detail


def test_every_eval_measure_reads_broken_when_the_source_is_gone():
    snap = _eval_snapshot([_run("a")])
    gone = measures.source_missing(snap)
    assert gone.eval_runs == [] and gone.source("eval-store").status == "error"
    for kid in ("gated-pass-rate", "cost-per-verified-case", "measurement-freshness-days",
                "error-rate", "cost-per-run-by-model"):
        r = measures.measure(kid, EVAL, [snap, gone])
        assert r.state == "broken" and r.value is None and r.reason, kid
    with pytest.raises(KeyError, match="no measure"):
        measures.measure("unmeasured-code-versions", EVAL, [snap])


# --- the simulated measures agree with the independently-derived ledger ---------------------


def test_simulated_measures_agree_with_the_ledger_and_refuse_gaps(tmp_path):
    """The measures no longer delegate to the ledger (RC1-310): the two are
    written separately, so agreement is checked the way the `kpi-ledger` eval
    checks it — value within tolerance, same state, same tripped."""
    prog, series = _sim_series(tmp_path, 20)
    truth = ledger.derive()
    for kid in ledger.KPI_IDS:
        got, want = measures.measure(kid, prog, series), truth.reading(20, kid)
        assert (got.state, got.tripped) == (want.state, want.tripped), kid
        if want.value is None:
            assert got.value is None, kid
        else:
            assert got.value == pytest.approx(want.value, abs=ledger.BY_ID[kid].tolerance), kid
    gone = measures.source_missing(series[-1])
    r = measures.measure("scope-change-pct", prog, series[:-1] + [gone])
    assert r.state == "broken" and r.value == truth.reading(19, "scope-change-pct").value
    gappy = series[:10] + series[12:]
    r = measures.measure("forecast-slip-days", prog, gappy)
    assert r.state == "broken" and "day(s) 10, 11" in r.reason


# --- verify: what the code enforces ------------------------------------------------------------


def test_verify_refuses_unknown_fields_downgrades_unmeasured_and_keeps_honest_verdicts():
    tree = instrument.load_adopted_tree("eval-run-store")
    snap = _eval_snapshot([_run("a"), _run("b", model=None)])
    verdicts = instrument.Verdicts(program="eval-run-store", verdicts=[
        _verdict("gated-pass-rate",
                 fields=["eval-store.passed", "eval-store.cases", "eval-store.errored"]),
        _verdict("cost-per-verified-case", "proxied",
                 fields=["eval-store.cost_usd", "constants.store_plan_usd_per_month"],
                 proxy="model spend plus a declared plan price",
                 misses="plan changes and overage until the constant is edited"),
        _verdict("measurement-freshness-days",
                 fields=["eval-store.started_at", "clock.sim_date"]),
        _verdict("unmeasured-code-versions", "rejected",
                 reason="GitHub tags are not a snapshot source"),
        _verdict("error-rate", fields=["eval-store.advisory_share"]),  # not in the catalog
        _verdict("cost-per-run-by-model", "proxied", fields=["eval-store.cost_usd"],
                 proxy="x"),  # no misses
    ], notes=["advisory share cannot be measured"])
    inst = instrument.verify(verdicts, tree, EVAL, [snap], model="claude-test", now=NOW)
    by = {k.kpi_id: k for k in inst.kpis}
    assert by["gated-pass-rate"].status == "verified" and by["gated-pass-rate"].sample.value == 90.0
    assert by["gated-pass-rate"].when_missing.state == "broken"
    assert by["cost-per-verified-case"].status == "verified"
    assert by["cost-per-verified-case"].verdict == "proxied"
    assert by["unmeasured-code-versions"].status == "rejected"
    assert by["error-rate"].status == "unverified"
    assert "not in the catalog: eval-store.advisory_share" in by["error-rate"].problems[0]
    assert by["cost-per-run-by-model"].status == "unverified"
    assert any("what it misses" in p for p in by["cost-per-run-by-model"].problems)
    assert inst.computes == [
        "gated-pass-rate", "cost-per-verified-case", "measurement-freshness-days",
    ]
    assert inst.rubric_version == 2 and inst.model == "claude-test"
    assert inst.sample_sim_date == TODAY.isoformat()
    assert inst.notes == ["advisory share cannot be measured"]


def test_verify_a_confirmed_kpi_with_no_measure_is_unverified_not_shipped():
    tree = instrument.load_adopted_tree("eval-run-store")
    verdicts = instrument.Verdicts(program="eval-run-store", verdicts=[
        _verdict("unmeasured-code-versions", fields=["eval-store.code_version"]),
    ])
    inst = instrument.verify(verdicts, tree, EVAL, [_eval_snapshot([_run("a")])], now=NOW)
    by = {k.kpi_id: k for k in inst.kpis}
    assert by["unmeasured-code-versions"].status == "unverified"
    assert "no measure is registered" in by["unmeasured-code-versions"].problems[0]
    # KPIs the model said nothing about are unverified too, never silently confirmed
    assert by["gated-pass-rate"].status == "unverified"
    assert by["gated-pass-rate"].problems == ["no verdict"]
    assert inst.computes == []


def test_verify_requires_broken_or_stale_when_sources_are_removed(monkeypatch):
    tree = instrument.load_adopted_tree("eval-run-store")

    def liar(program, series):
        from kpi.reading import Reading

        return Reading(kpi_id="error-rate", sim_date=series[-1].sim_date, value=0.0)

    monkeypatch.setitem(measures.MEASURES, "error-rate", liar)
    verdicts = instrument.Verdicts(program="eval-run-store", verdicts=[
        _verdict("error-rate", fields=["eval-store.errored", "eval-store.cases"]),
    ])
    inst = instrument.verify(verdicts, tree, EVAL, [_eval_snapshot([_run("a")])], now=NOW)
    k = next(k for k in inst.kpis if k.kpi_id == "error-rate")
    assert k.status == "unverified" and "still read 0.0 as ok" in k.problems[0]


def test_simulated_tree_instruments_end_to_end_with_a_fake_model(tmp_path):
    prog, series = _sim_series(tmp_path, 16)
    tree = instrument.load_adopted_tree("simulated-program")
    fields = {
        "forecast-slip-days": ["jira.status", "jira.points", "jira.due", "clock.sim_day"],
        "cost-vs-envelope": ["spend.planned_usd", "spend.actual_usd"],
        "scope-change-pct": ["jira.points", "jira.key"],
        "critical-path-slack-days": ["jira.links", "jira.start", "jira.due", "jira.labels"],
        "blocked-share-pct": ["jira.status", "jira.links", "jira.points", "jira.labels"],
        "weekly-spend-burn-ratio": ["spend.planned_usd", "spend.actual_usd"],
    }
    canned = dict(program="simulated-program", notes=[],
                  verdicts=[_verdict(k, fields=f) for k, f in fields.items()])
    client = _FakeClient(canned)
    cat = catalog.catalog(prog, series[-1])
    verdicts = instrument.propose(tree, cat, client=client, model="claude-test")
    payload = json.loads(client.captured["messages"][0]["content"])
    assert payload["source_catalog"] == cat
    assert payload["rubric"].startswith("# KPI rubric — version 2")
    assert client.captured["output_config"]["format"]["schema"] == instrument.SCHEMA
    assert instrument.last_usage == instrument.CallUsage(5000, 2000)

    inst = instrument.verify(verdicts, tree, prog, series, model="claude-test", now=NOW)
    assert inst.computes == list(ledger.KPI_IDS)
    truth = ledger.derive()
    for k in inst.kpis:
        want = truth.reading(16, k.kpi_id)
        assert (k.sample.state, k.sample.tripped) == (want.state, want.tripped), k.kpi_id
        if want.value is None:
            assert k.sample.value is None, k.kpi_id
        else:
            assert k.sample.value == pytest.approx(
                want.value, abs=ledger.BY_ID[k.kpi_id].tolerance
            ), k.kpi_id
        assert k.when_missing.state in ("broken", "stale")
    md = instrument.render_markdown(inst, tree)
    assert "6 of 6 compute from snapshots" in md
    assert "| `scope-change-pct` | confirmed | **verified** |" in md
    twin = instrument.write_instrumentation(inst, tree, tmp_path / "sim.md")
    assert instrument.Instrumentation.model_validate_json(twin.read_text()) == inst


def test_mismatched_rubric_version_refuses_to_propose():
    tree = instrument.load_adopted_tree("eval-run-store")
    with pytest.raises(define.DefineError, match="v99"):
        instrument.propose(tree, {}, rubric="# KPI rubric — version 99\n", client=_FakeClient({}))


def test_cli_needs_a_key_and_a_snapshot(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("config.settings.anthropic_api_key", None)
    assert instrument.main(["--program", "eval-run-store", "--out", str(tmp_path / "x.md")]) == 2
    monkeypatch.setattr("config.settings.anthropic_api_key", "k")
    assert instrument.main(["--program", "eval-run-store", "--out", str(tmp_path / "x.md"),
                            "--db", str(tmp_path / "empty.db")]) == 2
    assert "run `python -m collectors snapshot eval-run-store` first" in capsys.readouterr().err


def test_schema_names_exactly_the_verdict_fields():
    assert set(instrument._VERDICT["properties"]) == set(instrument.KpiVerdict.model_fields)
    assert set(instrument.SCHEMA["properties"]) == set(instrument.Verdicts.model_fields)
