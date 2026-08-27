"""Escalate stage — offline, no Postgres, no Slack, no model (RC1-307).

The done-when is tested where it can silently rot: the planted week-7
source break must be caught on the day it happens, from the snapshot
alone, with the blast radius named and a fix proposed — and no reading on
a break day may carry a zero for an unmeasurable KPI. The retry is tested
on both sides: a collector re-run that heals the source stores the fresh
snapshot and re-tracks the day; one that does not leaves the escalation
standing and stores nothing.

The Postgres store is exercised through its pure halves, the way the
readings and briefs stores are tested.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from collectors import programs
from collectors.models import BillingRow, ProgramSnapshot, SourceHealth
from kpi import escalate, measures, narrate, track
from kpi.escalations_store import (
    _COLUMNS,
    SCHEMA,
    UPSERT,
    Escalation,
    escalation_from_row,
    row_for,
)
from kpi.instrument import load_adopted_tree
from kpi.reading import Reading
from simulate import collected
from store.snapshot_store import SnapshotStore
from tests.test_kpi_instrument import EVAL, _eval_snapshot, _run

SIM = programs.get("simulated-program")
BREAK_DAY = 43  # the scenario's silent source break: label dropped from every story

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
TODAY = NOW.date()


def _billing_rows(today: date) -> list[BillingRow]:
    rows = [
        BillingRow(
            source="anthropic-costs", period_start=today - timedelta(days=i + 1),
            period_end=today - timedelta(days=i), amount_usd=1.0,
        )
        for i in range(5)
    ]
    rows.append(
        BillingRow(
            source="heroku-invoices", period_start=today - timedelta(days=30),
            period_end=today - timedelta(days=1), amount_usd=13.0, kind="invoice",
        )
    )
    return rows


def _full_eval_snapshot(runs, *, today: date = TODAY, status: str = "ok") -> ProgramSnapshot:
    """An eval-run-store snapshot faithful to production: billing feeds
    present and healthy, so every shipping KPI computes."""
    snap = _eval_snapshot(runs, today=today, status=status)
    return snap.model_copy(
        update={
            "billing": _billing_rows(today),
            "health": [
                *snap.health,
                SourceHealth(source="anthropic-costs", status="ok", count=5),
                SourceHealth(source="heroku-invoices", status="ok", count=1),
            ],
        }
    )


def _sim_series(last_day: int) -> list[ProgramSnapshot]:
    return [collected.program_snapshot(d, collected_at=NOW) for d in range(last_day + 1)]


def _sim_context(last_day: int):
    tree = load_adopted_tree(SIM.id)
    inst = track.load_instrumentation(SIM.id)
    series = _sim_series(last_day)
    readings = track.track(SIM, series, inst.computes)
    return tree, inst, series, readings


# --- the done-when: the planted break, caught within one tick --------------------------------


def test_the_planted_source_break_is_caught_on_the_day_it_happens():
    tree, inst, series, readings = _sim_context(BREAK_DAY)

    escalations = escalate.detect(SIM, tree, inst, series, readings, run_id=BREAK_DAY)

    (esc,) = [e for e in escalations if e.kind == "source"]
    assert esc.subject == "jira"
    assert esc.sim_date == series[-1].sim_date
    assert "jira read missing" in esc.reason
    assert "label" in esc.proposed_fix and "re-snapshot" in esc.proposed_fix


def test_a_partial_drop_below_half_is_the_same_break_seen_early():
    """The label dropped from most stories but not all: health still reads ok,
    and only the count-drop rule sees the silent break."""
    from collectors.models import ProjectSnapshot

    tree = load_adopted_tree(SIM.id)
    inst = track.load_instrumentation(SIM.id)
    prev = collected.program_snapshot(BREAK_DAY - 1, collected_at=NOW)
    kept = prev.jira.issues[:3]
    today = prev.model_copy(
        update={
            "sim_day": BREAK_DAY,
            "jira": ProjectSnapshot(project_key=prev.jira.project_key, issues=kept),
            "health": [
                h if h.source != "jira"
                else SourceHealth(source="jira", status="ok", count=len(kept))
                for h in prev.health
            ],
        }
    )
    series = [*_sim_series(BREAK_DAY - 1), today]

    escalations = escalate.detect(SIM, tree, inst, series, [], run_id=BREAK_DAY)

    (esc,) = [e for e in escalations if e.kind == "source"]
    assert esc.subject == "jira"
    assert "went quiet without erroring" in esc.reason
    assert f"{len(kept)} row(s)" in esc.reason
    assert "label" in esc.proposed_fix


def test_the_break_names_its_blast_radius_from_the_instrument_report():
    tree, inst, series, readings = _sim_context(BREAK_DAY)

    (esc,) = [
        e
        for e in escalate.detect(SIM, tree, inst, series, readings, run_id=BREAK_DAY)
        if e.kind == "source"
    ]

    jira_kpis = {
        "forecast-slip-days", "scope-change-pct", "critical-path-slack-days",
        "blocked-share-pct",
    }
    assert set(esc.kpi_ids) == jira_kpis


def test_no_zero_is_written_for_an_unmeasurable_kpi_on_the_break_day():
    tree, inst, series, readings = _sim_context(BREAK_DAY)

    (esc,) = [
        e
        for e in escalate.detect(SIM, tree, inst, series, readings, run_id=BREAK_DAY)
        if e.kind == "source"
    ]
    affected = [r for r in readings if r.kpi_id in esc.kpi_ids]

    assert affected
    for r in affected:
        assert r.state != "ok", f"{r.kpi_id} read ok through the source break"
        assert r.value != 0, f"{r.kpi_id} reported zero for unmeasurable"
        assert r.reason


def test_the_day_before_the_break_escalates_nothing():
    tree, inst, series, readings = _sim_context(BREAK_DAY - 1)
    assert escalate.detect(SIM, tree, inst, series, readings, run_id=BREAK_DAY - 1) == []


# --- source detection edges ------------------------------------------------------------------


def test_a_source_that_never_answered_yet_is_not_an_escalation():
    """The spend line is legitimately empty until week 1 lands — stale
    readings say so; escalating it would teach everyone to ignore the
    channel."""
    tree, inst, series, readings = _sim_context(2)
    spend = series[-1].source("spend")
    assert spend is not None and spend.status == "missing"

    assert escalate.detect(SIM, tree, inst, series, readings, run_id=2) == []


def test_an_errored_source_escalates_with_the_key_named_when_the_detail_names_it():
    tree = load_adopted_tree(EVAL.id)
    inst = track.load_instrumentation(EVAL.id)
    good = _eval_snapshot([_run("a"), _run("b")])
    bad = ProgramSnapshot(
        program_id=EVAL.id, collected_at=NOW, sim_date=TODAY,
        health=[
            SourceHealth(
                source="eval-store", status="error", detail="EVAL_DATABASE_URL is not set"
            )
        ],
    )
    series = [good, bad]
    readings = track.track(EVAL, series, inst.computes)

    escalations = escalate.detect(EVAL, tree, inst, series, readings, run_id=2)

    (esc,) = [e for e in escalations if e.kind == "source" and e.subject == "eval-store"]
    assert "EVAL_DATABASE_URL" in esc.proposed_fix
    assert set(esc.kpi_ids) >= {"gated-pass-rate", "error-rate"}


def test_billing_feeds_map_to_the_billing_field_prefix():
    inst = track.load_instrumentation(EVAL.id)
    assert "real-cost-per-run" in escalate.blast_radius(inst, "anthropic-costs")
    assert "real-cost-per-run" in escalate.blast_radius(inst, "heroku-invoices")


# --- reading detection: a shape change proposes the instrumentation fix ----------------------


def test_a_measure_that_raises_a_shape_error_proposes_re_instrumenting(monkeypatch):
    def shape_changed(program, series):
        raise TypeError("'NoneType' object is not subscriptable")

    monkeypatch.setitem(measures.MEASURES, "gated-pass-rate", shape_changed)
    tree = load_adopted_tree(EVAL.id)
    inst = track.load_instrumentation(EVAL.id)
    series = [_eval_snapshot([_run("a")])]
    readings = track.track(EVAL, series, ["gated-pass-rate"])

    escalations = escalate.detect(EVAL, tree, inst, series, readings, run_id=1)

    (esc,) = escalations
    assert esc.kind == "reading" and esc.subject == "gated-pass-rate"
    assert "kpi.instrument" in esc.proposed_fix


def test_a_broken_reading_explained_by_a_source_escalation_is_not_doubled():
    tree, inst, series, readings = _sim_context(BREAK_DAY)
    escalations = escalate.detect(SIM, tree, inst, series, readings, run_id=BREAK_DAY)

    source_radius = {
        k for e in escalations if e.kind == "source" for k in e.kpi_ids
    }
    reading_subjects = {e.subject for e in escalations if e.kind == "reading"}
    assert not (source_radius & reading_subjects)


# --- flatline --------------------------------------------------------------------------------


def _flat_history(kpi_id: str, value: float, days: int) -> dict[str, list[Reading]]:
    return {
        kpi_id: [
            Reading(
                kpi_id=kpi_id, sim_date=TODAY - timedelta(days=days - i), value=value,
                state="ok",
            )
            for i in range(days)
        ]
    }


def test_a_value_stuck_past_twice_its_cadence_is_a_flatline():
    tree = load_adopted_tree(EVAL.id)
    inst = track.load_instrumentation(EVAL.id)
    series = [_eval_snapshot([_run("a")])]
    readings = track.track(EVAL, series, [])

    escalations = escalate.detect(
        EVAL, tree, inst, series, readings, run_id=1,
        history=_flat_history("gated-pass-rate", 50.0, days=20),
    )

    (esc,) = escalations
    assert esc.kind == "flatline" and esc.subject == "gated-pass-rate"
    assert "50" in esc.reason
    assert "stuck sensor" in esc.proposed_fix


def test_a_kpi_resting_at_its_ideal_boundary_is_not_a_flatline():
    """An error rate at 0 for a month is a program behaving; a pass rate at
    100 likewise. Escalating health as sickness trains people to unsubscribe."""
    tree = load_adopted_tree(EVAL.id)
    inst = track.load_instrumentation(EVAL.id)
    series = [_eval_snapshot([_run("a")])]
    readings = track.track(EVAL, series, [])

    history = _flat_history("error-rate", 0.0, days=30)
    history.update(_flat_history("gated-pass-rate", 100.0, days=30))

    assert escalate.detect(
        EVAL, tree, inst, series, readings, run_id=1, history=history
    ) == []


def test_a_short_or_moving_series_is_not_a_flatline():
    tree = load_adopted_tree(EVAL.id)
    inst = track.load_instrumentation(EVAL.id)
    series = [_eval_snapshot([_run("a")])]
    readings = track.track(EVAL, series, [])

    short = _flat_history("gated-pass-rate", 50.0, days=3)
    moving = _flat_history("gated-pass-rate", 50.0, days=20)
    moving["gated-pass-rate"][-1] = Reading(
        kpi_id="gated-pass-rate", sim_date=TODAY, value=51.0, state="ok"
    )

    for history in (short, moving):
        assert escalate.detect(
            EVAL, tree, inst, series, readings, run_id=1, history=history
        ) == []


# --- implausible -----------------------------------------------------------------------------


def test_a_percentage_past_100_is_implausible(monkeypatch):
    def impossible(program, series):
        return Reading(kpi_id="gated-pass-rate", sim_date=TODAY, value=250.0, state="ok")

    monkeypatch.setitem(measures.MEASURES, "gated-pass-rate", impossible)
    tree = load_adopted_tree(EVAL.id)
    inst = track.load_instrumentation(EVAL.id)
    series = [_eval_snapshot([_run("a")])]
    readings = track.track(EVAL, series, ["gated-pass-rate"])

    escalations = escalate.detect(EVAL, tree, inst, series, readings, run_id=1)

    (esc,) = escalations
    assert esc.kind == "implausible"
    assert "250" in esc.reason and "cannot be true" in esc.reason
    assert "kpi.instrument" in esc.proposed_fix


def test_a_plausible_value_is_left_alone():
    tree = load_adopted_tree(EVAL.id)
    inst = track.load_instrumentation(EVAL.id)
    series = [_full_eval_snapshot([_run("a"), _run("b")])]
    readings = track.track(EVAL, series, inst.computes)

    assert [r for r in readings if r.state == "ok"], "fixture should read ok"
    assert escalate.detect(EVAL, tree, inst, series, readings, run_id=1) == []


# --- the retry -------------------------------------------------------------------------------


def _store_series(tmp_path, snaps: list[ProgramSnapshot]) -> str:
    db = str(tmp_path / "drift.db")
    with SnapshotStore(db) as store:
        for snap in snaps:
            store.save_program_snapshot(snap, project_key=EVAL.project_key)
    return db


def _dead_store_snapshot(today: date = TODAY) -> ProgramSnapshot:
    """Billing healthy, eval store unreachable — one source down, not the day."""
    return ProgramSnapshot(
        program_id=EVAL.id, collected_at=NOW, sim_date=today,
        billing=_billing_rows(today),
        health=[
            SourceHealth(source="eval-store", status="error", detail="connection refused"),
            SourceHealth(source="anthropic-costs", status="ok", count=5),
            SourceHealth(source="heroku-invoices", status="ok", count=1),
        ],
    )


def test_a_retry_that_heals_stores_the_snapshot_and_retracks_the_day(tmp_path):
    good = _full_eval_snapshot([_run("a"), _run("b")], today=TODAY - timedelta(days=1))
    db = _store_series(tmp_path, [good, _dead_store_snapshot()])
    fresh = _full_eval_snapshot([_run("a"), _run("b")])

    result = escalate.escalate_program(
        EVAL, db_path=db, recollect=lambda: fresh
    )

    assert result.retried
    assert result.escalations == []
    (healed,) = [e for e in result.healed if e.subject == "eval-store"]
    assert healed.healed and "healed on retry" in healed.proposed_fix
    with SnapshotStore(db) as store:
        assert len(store.program_runs(EVAL.id)) == 3  # the fresh snapshot was kept
    assert result.run_id == 3
    assert all(r.state == "ok" for r in result.readings if r.kpi_id == "error-rate")


def test_a_retry_that_does_not_heal_leaves_the_escalation_standing(tmp_path):
    good = _full_eval_snapshot([_run("a")], today=TODAY - timedelta(days=1))
    db = _store_series(tmp_path, [good, _dead_store_snapshot()])

    result = escalate.escalate_program(
        EVAL, db_path=db, recollect=_dead_store_snapshot
    )

    assert result.retried and result.healed == []
    assert [e.subject for e in result.escalations if e.kind == "source"] == ["eval-store"]
    with SnapshotStore(db) as store:
        assert len(store.program_runs(EVAL.id)) == 2  # a useless retry is not kept


def test_no_recollect_means_no_retry(tmp_path):
    good = _full_eval_snapshot([_run("a")], today=TODAY - timedelta(days=1))
    db = _store_series(tmp_path, [good, _dead_store_snapshot()])

    result = escalate.escalate_program(EVAL, db_path=db)

    assert not result.retried
    assert [e.subject for e in result.escalations] == ["eval-store"]


# --- rendering -------------------------------------------------------------------------------


def test_the_alert_names_the_blast_radius_and_the_fix():
    tree, inst, series, readings = _sim_context(BREAK_DAY)
    escalations = escalate.detect(SIM, tree, inst, series, readings, run_id=BREAK_DAY)
    result = escalate.EscalateResult(
        program_id=SIM.id, run_id=BREAK_DAY, sim_date=series[-1].sim_date,
        readings=readings, escalations=escalations, healed=[], retried=True,
    )

    alert = escalate.render_alert(result, SIM.name)

    assert "blast radius" in alert and "`forecast-slip-days`" in alert
    assert "proposed fix" in alert and "label" in alert
    assert "never zeroed" in alert


# --- the store's pure halves -----------------------------------------------------------------


def _esc(**kw) -> Escalation:
    defaults = dict(
        program_id="simulated-program", sim_date=date(2026, 10, 20), kind="source",
        subject="jira", kpi_ids=("forecast-slip-days", "scope-change-pct"),
        reason="jira answered with 1 row(s) against 34", proposed_fix="restore the label",
        run_id=43,
    )
    return Escalation(**{**defaults, **kw})


def test_row_and_escalation_round_trip():
    esc = _esc()
    row = (*row_for(esc), esc.created_at)
    back = escalation_from_row(
        tuple(json.loads(v) if i == 4 else v for i, v in enumerate(row))
    )
    assert back == esc


def test_the_row_matches_the_select_columns():
    assert len(row_for(_esc())) + 1 == len(_COLUMNS.split(", "))


def test_the_schema_refuses_an_escalation_with_no_reason_or_fix():
    assert "CHECK (reason <> '')" in SCHEMA
    assert "CHECK (proposed_fix <> '')" in SCHEMA
    assert "kind IN ('source', 'reading', 'flatline', 'implausible')" in SCHEMA
    with pytest.raises(ValueError, match="reason and proposed_fix"):
        _esc(reason="")
    with pytest.raises(ValueError, match="unknown escalation kind"):
        _esc(kind="vibes")


def test_a_reposted_day_keeps_its_original_posted_stamp():
    assert "COALESCE" in UPSERT and "posted_at" in UPSERT


# --- the brief carries the escalations -------------------------------------------------------


def test_the_payload_and_brief_carry_the_escalations():
    esc = _esc()
    entry = narrate.escalation_entry(esc)
    payload = {
        "program": "Observability Platform GA (simulated)",
        "week_ending": "2026-10-20",
        "run_id": 43,
        "kpis": [],
        "not_shipping": [],
        "escalations": [entry],
    }
    data = {"headline": "h", "outcome_lines": [], "movement": "m", "asks": []}

    brief = narrate.render_brief(payload, data)

    assert "*Escalations this week*" in brief
    assert "source: jira" in brief
    assert "`forecast-slip-days`" in brief
    assert "restore the label" in brief
    # The escalation's own numbers are payload numbers: the audit vouches.
    assert narrate.audit_numbers(["jira answered with 1 row(s) against 34"], payload) == []


def test_a_week_with_no_escalations_renders_no_section():
    payload = {
        "program": "p", "week_ending": "2026-10-20", "run_id": 1,
        "kpis": [], "not_shipping": [], "escalations": [],
    }
    data = {"headline": "h", "outcome_lines": [], "movement": "m", "asks": []}
    assert "Escalations" not in narrate.render_brief(payload, data)
