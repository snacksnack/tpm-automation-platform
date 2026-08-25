"""Narrate stage — offline, no Postgres, no Slack, no live model (RC1-306).

What is tested is the part of the brief that can be quietly wrong: that the
payload is number-complete and outcomes-first, that the audit actually
refuses a brief whose prose contains a number the payload cannot vouch for,
that stale and proxied KPIs come out labelled rather than smoothed over,
and that the archive's pure halves round-trip. The model is a canned fake,
the way the define/instrument tests do it — the prose is Claude's job, the
numbers are not, and only the second half is testable.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from collectors import programs
from kpi import instrument, narrate, track
from kpi.briefs_store import SCHEMA, Brief, brief_from_row, row_for
from kpi.reading import Reading
from kpi.readings_store import StoredReading

EVAL = programs.get("eval-run-store")
WEEK_ENDING = date(2026, 8, 25)
NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def tree():
    return instrument.load_adopted_tree("eval-run-store")


@pytest.fixture(scope="module")
def inst():
    return track.load_instrumentation("eval-run-store")


def _stored(kpi_id, sim_date, value, *, state="ok", tripped=False, run_id=7, reason=None):
    return StoredReading(
        program_id="eval-run-store",
        run_id=run_id,
        computed_at=NOW,
        reading=Reading(
            kpi_id=kpi_id, sim_date=sim_date, value=value, state=state, tripped=tripped,
            reason=reason,
        ),
    )


def _two_weeks(tree, inst) -> list[StoredReading]:
    """Daily readings for every shipping KPI, last week's run_id below this week's."""
    out = []
    for kpi_id in inst.computes:
        for back in range(14, -1, -1):
            day = WEEK_ENDING - timedelta(days=back)
            out.append(
                _stored(kpi_id, day, 80.0 + back, run_id=7 if back == 0 else 6 - back // 7)
            )
    return out


# --- the payload -----------------------------------------------------------------------------


def test_payload_is_outcomes_first_and_number_complete(tree, inst):
    payload = narrate.build_payload(EVAL, tree, inst, _two_weeks(tree, inst))
    kinds = [k["kind"] for k in payload["kpis"]]
    assert kinds == sorted(kinds, key=lambda k: k != "outcome"), "outcomes must come first"
    assert payload["week_ending"] == WEEK_ENDING.isoformat()
    assert payload["run_id"] == 7  # the narrated day's run, not an older one
    entry = payload["kpis"][0]
    assert entry["latest"]["value"] == 80.0 and entry["latest"]["run_id"] == 7
    assert entry["week_ago"]["value"] == 87.0  # exactly seven days back
    assert entry["delta"] == pytest.approx(-7.0)
    assert {"unit", "direction", "so_what", "leads"} <= set(entry)


def test_a_tree_kpi_that_does_not_ship_is_named_not_silently_dropped(tree, inst):
    gutted = inst.model_copy(
        update={"kpis": [k for k in inst.kpis if k.kpi_id != "error-rate"]}
    )
    payload = narrate.build_payload(EVAL, tree, gutted, _two_weeks(tree, gutted))
    gaps = {g["kpi_id"] for g in payload["not_shipping"]}
    assert "error-rate" in gaps
    assert all(k["kpi_id"] != "error-rate" for k in payload["kpis"])


def test_first_week_has_no_delta_and_flags_fire_on_change(tree, inst):
    one_day = [_stored(k, WEEK_ENDING, 50.0) for k in inst.computes]
    payload = narrate.build_payload(EVAL, tree, inst, one_day)
    entry = payload["kpis"][0]
    assert entry["week_ago"] is None and entry["delta"] is None

    turned = [_stored("gated-pass-rate", WEEK_ENDING - timedelta(days=7), 90.0)] + [
        _stored("gated-pass-rate", WEEK_ENDING, 50.0, tripped=True)
    ]
    entry = narrate.build_payload(EVAL, tree, inst, turned)["kpis"][0]
    assert entry["newly_tripped"] and not entry["newly_broken"]

    healed = [
        _stored("gated-pass-rate", WEEK_ENDING - timedelta(days=7), None,
                state="broken", reason="gone"),
        _stored("gated-pass-rate", WEEK_ENDING, 90.0),
    ]
    entry = narrate.build_payload(EVAL, tree, inst, healed)["kpis"][0]
    assert entry["recovered"]


def test_no_readings_refuses_rather_than_narrating_nothing(tree, inst):
    with pytest.raises(narrate.NarrateError, match="no readings"):
        narrate.build_payload(EVAL, tree, inst, [])


# --- the audit -------------------------------------------------------------------------------


def test_audit_allows_payload_numbers_rounded_counts_and_dates():
    payload = {"a": 2.0417, "b": 1420.0, "c": {"d": [96.3, "2026-08-25"]}}
    ok = [
        "The ratio hit 2.04 this week (2.0417 exactly), $1,420 over plan.",
        "96.3 % — three KPIs moved; by 2026-08-25 all twelve were green.",
    ]
    assert narrate.audit_numbers(ok, payload) == []


def test_audit_refuses_an_invented_number():
    payload = {"value": 50.0, "delta": -7.0}
    bad = narrate.audit_numbers(["Pass rate fell 14 % to 43."], payload)
    assert bad == ["14", "43"]


def test_audit_catches_model_arithmetic_not_just_fabrication():
    # 87 - 80 is true arithmetic, but 7... is a small count; use bigger numbers.
    payload = {"latest": 87.4, "week_ago": 60.1}
    assert narrate.audit_numbers(["up 27.3 points"], payload) == ["27.3"]


# --- rendering -------------------------------------------------------------------------------


def _entry(kpi_id="gated-pass-rate", *, kind="outcome", value=50.0, state="ok",
           tripped=False, reason=None, proxy=None, caveat=None, delta=-7.0,
           unit="% of scorable cases", name="Gated pass rate"):
    return {
        "kpi_id": kpi_id, "name": name, "kind": kind, "unit": unit, "direction": "higher",
        "so_what": "freeze on two consecutive", "leads": None,
        "latest": {"sim_date": "2026-08-25", "value": value, "state": state,
                   "tripped": tripped, "as_of": "2026-08-25", "reason": reason,
                   "detail": "", "run_id": 7},
        "week_ago": {"sim_date": "2026-08-18", "value": 57.0, "state": "ok",
                     "tripped": False},
        "delta": delta, "series": [], "newly_tripped": False, "newly_broken": False,
        "recovered": False, "proxy": proxy, "caveat": caveat,
    }


def _payload(entries, not_shipping=()):
    return {
        "program_id": "eval-run-store", "program": "Eval run store",
        "week_ending": "2026-08-25", "run_id": 7, "kpis": entries,
        "not_shipping": list(not_shipping),
    }


def _narrative(**over):
    base = {
        "headline": "Pass rate fell.",
        "outcome_lines": [{"kpi_id": "gated-pass-rate", "line": "worth watching"}],
        "movement": "freshness explains it",
        "asks": ["Decide the freeze."],
    }
    base.update(over)
    return base


def test_render_puts_outcomes_above_the_fold_and_traces_the_numbers():
    text = narrate.render_brief(_payload([_entry()]), _narrative())
    assert text.index("*Outcomes*") < text.index("*What moved") < text.index("*Asks*")
    assert "🟢 *Gated pass rate*: 50 % (-7 w/w) — worth watching" in text
    assert "snapshot run 7" in text and "2026-08-25" in text


def test_render_labels_stale_broken_and_proxied_rather_than_smoothing():
    entries = [
        _entry(value=None, state="stale", reason="latest run is 9 days old"),
        _entry(kpi_id="cost-per-verified-case", name="Cost per verified case",
               unit="USD per scored case", value=0.02, proxy="plan price",
               caveat="declared, not measured", delta=None),
    ]
    text = narrate.render_brief(_payload(entries), _narrative())
    assert "🟡 *Gated pass rate*: —" in text
    assert "[stale: latest run is 9 days old]" in text
    assert "$0.02" in text and "[proxy: declared, not measured]" in text


def test_render_marks_tripped_and_names_the_gaps():
    text = narrate.render_brief(
        _payload([_entry(tripped=True)],
                 not_shipping=[{"kpi_id": "unmeasured-code-versions", "kind": "leading",
                                "why": "not instrumented"}]),
        _narrative(asks=[]),
    )
    assert "🔴 *Gated pass rate*" in text
    assert "*Asks*" not in text
    assert "_Not in this brief: unmeasured-code-versions (not instrumented)._" in text


# --- narrate() end to end with a fake model --------------------------------------------------


class _FakeClient:
    def __init__(self, canned: dict):
        self._canned = canned
        self.captured: dict = {}
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.captured = kwargs
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(self._canned))],
            usage=SimpleNamespace(input_tokens=9000, output_tokens=700),
        )


def test_narrate_builds_an_archived_shape_brief(tree, inst):
    client = _FakeClient(_narrative())
    brief = narrate.narrate(EVAL, tree, inst, _two_weeks(tree, inst), client=client)
    assert isinstance(brief, Brief)
    assert brief.week_ending == WEEK_ENDING and brief.run_id == 7
    assert brief.prompt_version == narrate.prompt_version() == 1
    assert brief.payload == json.loads(client.captured["messages"][0]["content"])
    assert client.captured["output_config"]["format"]["schema"] == narrate.SCHEMA
    assert "Weekly KPI brief" in brief.brief
    assert narrate.last_usage == narrate.CallUsage(9000, 700)


def test_a_brief_with_an_invented_number_is_refused_not_posted(tree, inst):
    client = _FakeClient(_narrative(headline="Pass rate is 41.7 % now."))
    with pytest.raises(narrate.NarrateError, match=r"rejected.*41\.7"):
        narrate.narrate(EVAL, tree, inst, _two_weeks(tree, inst), client=client)


# --- the archive's pure halves ---------------------------------------------------------------


def test_brief_row_round_trips():
    brief = Brief(
        program_id="eval-run-store", week_ending=WEEK_ENDING, run_id=7,
        payload={"run_id": 7}, narrative={"headline": "h"}, brief="text",
        model="claude-opus-5", prompt_version=1,
    )
    row = row_for(brief)
    back = brief_from_row((*row, NOW))
    assert back.payload == brief.payload and back.narrative == brief.narrative
    assert back.brief == "text" and back.posted_at is None and back.created_at == NOW


def test_the_archive_keeps_a_posted_stamp_once_set():
    """A re-narrated week must not quietly un-post itself: the UPSERT keeps
    the first posted_at, and mark_posted is the only writer of a new one."""
    from kpi.briefs_store import UPSERT

    assert "COALESCE(kpi_briefs.posted_at, EXCLUDED.posted_at)" in UPSERT
    assert "PRIMARY KEY (program_id, week_ending)" in SCHEMA


# --- CLI guard rails -------------------------------------------------------------------------


def test_cli_without_the_dsn_says_where_it_lives(monkeypatch, capsys):
    monkeypatch.delenv("EVAL_DATABASE_URL", raising=False)
    assert narrate.main(["--program", "eval-run-store"]) == 2
    assert "EVAL_DATABASE_URL" in capsys.readouterr().err


def test_cli_refuses_post_without_archive(monkeypatch, capsys):
    monkeypatch.setenv("EVAL_DATABASE_URL", "postgres://unused")
    real_tree = instrument.load_adopted_tree("eval-run-store")
    real_inst = track.load_instrumentation("eval-run-store")
    monkeypatch.setattr(narrate.instrument, "load_adopted_tree", lambda pid: real_tree)
    monkeypatch.setattr(narrate.track, "load_instrumentation", lambda pid: real_inst)
    stored = _two_weeks(real_tree, real_inst)

    class FakeReadings:
        def __init__(self, dsn):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def readings(self, program_id):
            return stored

    monkeypatch.setattr(narrate, "ReadingsStore", FakeReadings)
    monkeypatch.setattr(
        narrate, "_default_client", lambda: _FakeClient(_narrative())
    )
    rc = narrate.main(["--program", "eval-run-store", "--no-archive", "--post"])
    assert rc == 2
    assert "never posted" in capsys.readouterr().err


def test_prose_artifacts_are_stripped_before_rendering(tree, inst):
    dirty = _narrative(
        headline="Pass  rate fell.{",
        asks=["  Decide the freeze. ", "  "],
    )
    client = _FakeClient(dirty)
    brief = narrate.narrate(EVAL, tree, inst, _two_weeks(tree, inst), client=client)
    assert "Pass rate fell." in brief.brief and "{" not in brief.brief
    assert "• Decide the freeze." in brief.brief
    assert brief.narrative["asks"] == ["Decide the freeze."]
