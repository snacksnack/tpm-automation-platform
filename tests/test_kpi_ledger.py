"""Ground-truth ledger and the `kpi-ledger` subject — offline (RC1-300).

Three properties carry the story: the ledger is derived (the committed CSV is
exactly what `derive()` produces today), it reads the planted events the way
the tree says it must (detector by the day after, everything else within the
forecast window), and the suite can fail — a deliberately wrong implementation
fails it on precisely the days it is wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals import kpi_ledger
from kpi.reading import Reading
from simulate import ledger, scenario
from simulate.scenario import GA_DAY, LAST_DAY

CSV = Path(__file__).resolve().parent.parent / "docs" / "kpi" / "ledger" / "simulated-program.csv"


@pytest.fixture(scope="module")
def book() -> ledger.Ledger:
    return ledger.derive()


# --- the reading contract ----------------------------------------------------------------


def test_a_reading_that_is_not_ok_must_say_why():
    with pytest.raises(ValueError, match="needs a reason"):
        Reading(kpi_id="x", sim_date=scenario.sim_date(0), value=None, state="stale")
    Reading(kpi_id="x", sim_date=scenario.sim_date(0), value=None, state="stale", reason="why")


# --- shape ---------------------------------------------------------------------------------


def test_ledger_covers_every_day_and_every_adopted_kpi(book):
    assert book.days == list(range(LAST_DAY + 1))
    assert len(book.rows) == (LAST_DAY + 1) * 6
    adopted = {
        "forecast-slip-days", "cost-vs-envelope", "scope-change-pct",
        "critical-path-slack-days", "blocked-share-pct", "weekly-spend-burn-ratio",
    }
    assert set(ledger.KPI_IDS) == adopted
    for day in book.days:
        assert {r.kpi_id for r in book.readings(day)} == adopted
        assert all(r.sim_date == scenario.sim_date(day) for r in book.readings(day))


def test_committed_csv_is_what_derive_produces_today(book):
    """The ledger is derived, not typed: regenerate with
    `python -m simulate ledger --out docs/kpi/ledger/simulated-program.csv`."""
    assert CSV.read_text() == ledger.to_csv(book), (
        "docs/kpi/ledger/simulated-program.csv is stale — the scenario or a formula changed"
    )
    header = CSV.read_text().splitlines()[0]
    assert header.split(",") == list(ledger.COLUMNS)


# --- no zero for unknown ---------------------------------------------------------------------


def test_cost_kpis_are_stale_with_a_reason_before_the_first_row_lands(book):
    for day in range(0, 7):
        for kpi_id in ledger.SPEND_KPIS:
            r = book.reading(day, kpi_id)
            assert r.value is None and r.state == "stale" and "week 1 lands on day 7" in r.reason
    assert book.reading(7, "weekly-spend-burn-ratio").state == "ok"
    assert book.reading(7, "cost-vs-envelope").value == pytest.approx(-50.0)


def test_source_break_reads_broken_and_carries_day_42_with_its_date(book):
    good = {k: book.reading(42, k) for k in ledger.JIRA_KPIS}
    for day in range(43, 48):
        for kpi_id in ledger.JIRA_KPIS:
            r = book.reading(day, kpi_id)
            assert r.state == "broken", (day, kpi_id)
            assert r.value == good[kpi_id].value and r.tripped == good[kpi_id].tripped
            assert r.as_of == scenario.sim_date(42) and r.sim_date == scenario.sim_date(day)
            assert "since day 43" in r.reason and "34 issues" in r.reason
        for kpi_id in ledger.SPEND_KPIS:
            assert book.reading(day, kpi_id).state == "ok", (day, kpi_id)  # different source
    for kpi_id in ledger.JIRA_KPIS:
        r = book.reading(48, kpi_id)
        assert r.state == "ok" and r.as_of == scenario.sim_date(48)


def test_forecast_is_zero_once_everything_is_done_on_the_ga_day(book):
    for day in (GA_DAY, LAST_DAY):
        r = book.reading(day, "forecast-slip-days")
        assert r.value == 0.0 and r.state == "ok" and "day 67" in r.detail
    assert book.reading(GA_DAY - 1, "forecast-slip-days").value != 0.0


# --- the planted events, read the way the tree says -----------------------------------------


def test_scope_add_crosses_ten_percent_on_the_day_after(book):
    assert book.reading(15, "scope-change-pct").value == 0.0
    assert book.reading(16, "scope-change-pct").value == pytest.approx(9.63)
    r = book.reading(17, "scope-change-pct")
    assert r.value == pytest.approx(11.85) and r.tripped
    assert not book.reading(16, "scope-change-pct").tripped


def test_upstream_slip_inverts_the_ga_chain_the_same_day(book):
    before = book.reading(28, "critical-path-slack-days")
    after = book.reading(29, "critical-path-slack-days")
    assert before.value == 3.0 and not before.tripped
    assert after.value == -11.0 and after.tripped and "t-context" in after.detail
    # and clears once the upstream lands (day 41), leaving the next-tightest link
    assert book.reading(41, "critical-path-slack-days").value == 3.0
    # blocked share rises through weeks 5-6 as the chain stalls
    assert book.reading(29, "blocked-share-pct").value == 0.0
    assert book.reading(30, "blocked-share-pct").value > 15
    assert book.reading(41, "blocked-share-pct").value > 30
    assert "direct 5" in book.reading(30, "blocked-share-pct").detail


def test_cost_spike_trips_the_burn_ratio_the_day_the_row_lands(book):
    assert not book.reading(41, "weekly-spend-burn-ratio").tripped
    r = book.reading(42, "weekly-spend-burn-ratio")
    assert r.value == pytest.approx(2.0417, abs=1e-4) and r.tripped
    assert book.reading(42, "cost-vs-envelope").value == pytest.approx(1420.0)
    # the cumulative outcome trips only after two consecutive weeks over 110 %
    assert not book.reading(42, "cost-vs-envelope").tripped
    assert book.reading(49, "cost-vs-envelope").tripped


def test_every_detector_reacts_by_the_day_after_and_every_mover_within_the_window(book):
    seen = set()
    for r in ledger.reactions(book):
        seen.add((r.event_id, r.kpi_id))
        assert r.reacted_on is not None, f"{r.kpi_id} never reacted to {r.event_id}"
        if r.detector:
            assert r.lag <= 1, f"{r.kpi_id} saw {r.event_id} on day {r.reacted_on} (lag {r.lag})"
        else:
            assert r.lag <= ledger.WINDOW_DAYS, (r.event_id, r.kpi_id, r.lag)
    assert seen == {(e.id, k) for e in scenario.EVENTS for k in e.must_move}
    for e in scenario.EVENTS:
        assert e.detector and set(e.detector) <= set(e.must_move), e.id


def test_ga_chain_is_read_from_the_label_not_the_scenario(book):
    snap = ledger.snapshot(0)
    assert ledger.ga_chain(snap) == {
        "p-ga", "p-loadtest", "a-runbooks", "a-checkout-alerts", "s-dash-checkout",
        "s-latency", "t-context",
    }
    assert scenario.GA_BLOCKING_LABEL in snap.issues["p-ga"].labels
    assert not any(
        scenario.GA_BLOCKING_LABEL in i.labels for s, i in snap.issues.items() if s != "p-ga"
    )


# --- the suite ----------------------------------------------------------------------------------


def test_one_case_per_day_with_a_detection_check_the_day_after_each_event():
    assert len(kpi_ledger.CASES) == LAST_DAY + 1
    by_id = {c.id: c for c in kpi_ledger.CASES}
    for e in scenario.EVENTS:
        assert f"detects-{e.id}" in by_id[f"day-{e.day + 1:02d}"].expect
        assert f"detects-{e.id}" not in by_id[f"day-{e.day:02d}"].expect
    assert "source-break" in by_id["day-45"].tags and "week-7" in by_id["day-45"].tags


def test_reference_implementation_passes_every_case():
    results = kpi_ledger.run("reference")
    assert all(r.passed for r in results) and not any(r.error for r in results)
    assert all(r.observations["mismatches"] == 0 for r in results)


def test_an_implementation_that_trusts_an_empty_snapshot_fails_the_break_days():
    results = {r.case_id: r for r in kpi_ledger.run("no-break-detection")}
    failed = sorted(cid for cid, r in results.items() if not r.passed)
    assert failed == [f"day-{d}" for d in range(43, 48)]
    day44 = {c.name: c for c in results["day-44"].characteristics}
    assert not day44["detects-source-break"].passed
    assert "not broken" in day44["detects-source-break"].detail
    assert "state 'ok' vs expected 'broken'" in day44["scope-change-pct"].detail
    assert "-100" in day44["scope-change-pct"].detail  # the zero-for-unknown failure, in numbers


def test_the_rejected_28_day_window_fails_where_the_forecast_differs():
    results = kpi_ledger.run("window-28")
    failed = [r for r in results if not r.passed]
    assert 20 < len(failed) < 70
    assert all(
        {c.name for c in r.characteristics if not c.passed} == {"forecast-slip-days"}
        for r in failed
    )
    assert all(r.passed for r in results if r.case_id < "day-14")  # plan rate until day 14


def test_a_reading_with_the_wrong_shape_is_an_error_not_a_failure():
    def exploding(day: int):
        raise RuntimeError("no snapshot")

    result = kpi_ledger.run_case(kpi_ledger.CASES[3], exploding, ledger.derive())
    assert result.error == "RuntimeError: no snapshot" and not result.passed


def test_a_missing_kpi_fails_its_characteristic_by_name():
    truth = ledger.derive()
    partial = [r for r in truth.readings(10) if r.kpi_id != "blocked-share-pct"]
    result = kpi_ledger.run_case(kpi_ledger.CASES[10], lambda day: partial, truth)
    failed = {c.name: c.detail for c in result.characteristics if not c.passed}
    assert failed == {"blocked-share-pct": "no reading produced"}


def test_subject_version_names_the_implementation_only_when_it_is_not_the_reference():
    assert kpi_ledger.version("reference").prompt_version is None
    assert kpi_ledger.version("window-28").prompt_version == "impl:window-28"
    assert kpi_ledger.version("reference").model is None


# --- the CLIs ---------------------------------------------------------------------------------


def test_evals_cli_records_the_reference_and_not_the_wrong_ones(tmp_path, monkeypatch, capsys):
    from evals import __main__ as cli

    monkeypatch.setenv("EVAL_RUNS_PATH", str(tmp_path / "runs.jsonl"))
    monkeypatch.delenv("EVAL_DATABASE_URL", raising=False)
    assert cli.main(["run", "kpi-ledger"]) == 0
    assert "70/70 passed" in capsys.readouterr().out
    assert (tmp_path / "runs.jsonl").read_text().count("\n") == 1

    assert cli.main(["run", "kpi-ledger", "--impl", "no-break-detection"]) == 1
    out = capsys.readouterr().out
    assert "65/70 passed" in out and "not recorded" in out
    assert (tmp_path / "runs.jsonl").read_text().count("\n") == 1

    assert cli.main(["run", "kpi-ledger", "--impl", "nope"]) == 2


def test_simulate_cli_prints_and_writes_the_ledger(tmp_path, capsys):
    from simulate import __main__ as cli

    assert cli.main(["ledger", "--day", "43"]) == 0
    out = capsys.readouterr().out
    assert "broken" in out and "carrying day 42" in out
    target = tmp_path / "ledger.csv"
    assert cli.main(["ledger", "--out", str(target)]) == 0
    assert target.read_text() == ledger.to_csv(ledger.derive())
    assert "420 rows" in capsys.readouterr().out


def test_simulator_writes_the_ledger_beside_the_clock(tmp_path):
    from simulate.clock import SimState

    state = SimState(tmp_path / "sim")
    state.write(3, {"epic": "PMA-1"})
    assert state.ledger_path.read_text() == ledger.to_csv(ledger.derive())
    state.forget()
    assert not state.ledger_path.exists()
