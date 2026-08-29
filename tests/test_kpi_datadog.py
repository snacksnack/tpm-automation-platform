"""Datadog leg of the track stage — offline, no network (RC1-305 revisited).

What is tested is the honesty rule crossing the wire: an unmeasured KPI must
leave a *gap* in the value series, never a zero, while its state still ships
on the health metric. The payload builder is pure, so the tests read it the
way `test_kpi_track` reads `row_for` rather than mocking httpx; the POST
itself is one call verified by running it against the real account.
"""

from __future__ import annotations

from datetime import date

import pytest

from kpi import datadog
from kpi.reading import Reading

DAY = date(2026, 9, 12)
AT = 1_790_000_000


def _ok(kpi_id: str = "cost-vs-envelope", value: float = 1.2, tripped: bool = False) -> Reading:
    return Reading(kpi_id=kpi_id, sim_date=DAY, value=value, tripped=tripped)


def _stale(kpi_id: str = "weekly-spend-burn-ratio") -> Reading:
    return Reading(
        kpi_id=kpi_id, sim_date=DAY, value=None, state="stale",
        reason="no spend row has landed yet",
    )


def _broken(kpi_id: str = "blocked-share-pct") -> Reading:
    return Reading(
        kpi_id=kpi_id, sim_date=DAY, value=None, state="broken",
        reason="source removed from the snapshot",
    )


def _by_metric(series: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for s in series:
        out.setdefault(s["metric"], []).append(s)
    return out


# --- series_for ------------------------------------------------------------------------------


def test_ok_reading_ships_value_health_and_tripped():
    series = datadog.series_for([_ok(value=1.2)], program_id="simulated-program", at=AT)
    metrics = _by_metric(series)
    value = metrics["kpi.program.cost_vs_envelope"][0]
    assert value["points"] == [{"timestamp": AT, "value": 1.2}]
    assert value["tags"] == ["program:simulated-program"]
    assert metrics[datadog.HEALTH_METRIC][0]["points"][0]["value"] == 0
    assert metrics[datadog.TRIPPED_METRIC][0]["points"][0]["value"] == 0.0


def test_unmeasured_reading_is_a_gap_never_a_zero():
    series = datadog.series_for(
        [_stale(), _broken()], program_id="simulated-program", at=AT
    )
    metrics = _by_metric(series)
    # No value metric at all for either KPI — the chart shows a hole.
    assert not any(m.startswith("kpi.program.weekly") for m in metrics)
    assert not any(m.startswith("kpi.program.blocked") for m in metrics)
    healths = {
        next(t for t in s["tags"] if t.startswith("kpi:")): s["points"][0]["value"]
        for s in metrics[datadog.HEALTH_METRIC]
    }
    assert healths == {
        "kpi:weekly-spend-burn-ratio": datadog.HEALTH["stale"],
        "kpi:blocked-share-pct": datadog.HEALTH["broken"],
    }


def test_tripped_ships_as_one():
    series = datadog.series_for(
        [_ok(kpi_id="forecast-slip-days", value=6.0, tripped=True)],
        program_id="simulated-program",
        at=AT,
    )
    tripped = _by_metric(series)[datadog.TRIPPED_METRIC][0]
    assert tripped["points"][0]["value"] == 1.0
    assert "kpi:forecast-slip-days" in tripped["tags"]


def test_health_and_tripped_carry_program_and_kpi_tags():
    series = datadog.series_for([_ok()], program_id="eval-run-store", at=AT)
    for s in _by_metric(series)[datadog.HEALTH_METRIC]:
        assert "program:eval-run-store" in s["tags"]
        assert any(t.startswith("kpi:") for t in s["tags"])


def test_metric_name_folds_hyphens_the_way_datadog_will():
    assert datadog.metric_name("gated-pass-rate") == "kpi.program.gated_pass_rate"


def test_every_series_is_a_gauge():
    series = datadog.series_for(
        [_ok(), _stale(), _broken()], program_id="simulated-program", at=AT
    )
    assert {s["type"] for s in series} == {datadog.GAUGE}


# --- ship_readings ---------------------------------------------------------------------------


def test_without_an_api_key_the_leg_is_skipped(monkeypatch):
    monkeypatch.delenv("DD_API_KEY", raising=False)
    assert datadog.ship_readings([_ok()], program_id="simulated-program") is None


def test_with_a_key_the_series_and_event_counts_come_back(monkeypatch):
    monkeypatch.setenv("DD_API_KEY", "test-key")
    sent: dict = {}

    def fake_ship(series, *, api_key, site=None):
        sent["series"] = series
        sent["api_key"] = api_key

    def fake_ship_events(events, *, api_key, site=None):
        sent["events"] = events

    monkeypatch.setattr(datadog, "ship", fake_ship)
    monkeypatch.setattr(datadog, "ship_events", fake_ship_events)
    counts = datadog.ship_readings(
        [_ok(), _stale()], program_id="simulated-program", at=AT
    )
    # ok → value + health + tripped; stale → health + tripped; the stale
    # reading is also the one event with something to explain.
    assert counts == (5, 1)
    assert len(sent["series"]) == 5
    assert len(sent["events"]) == 1
    assert sent["api_key"] == "test-key"


# --- events_for ------------------------------------------------------------------------------


def test_an_ok_untripped_reading_posts_no_event():
    assert datadog.events_for([_ok()], program_id="simulated-program") == []


def test_the_why_rides_the_event():
    [event] = datadog.events_for([_stale()], program_id="simulated-program")
    assert event["title"] == "weekly-spend-burn-ratio is stale on simulated-program"
    assert "no spend row has landed yet" in event["text"]
    assert event["alert_type"] == "warning"
    assert event["aggregation_key"] == "kpi-reading:simulated-program:weekly-spend-burn-ratio"


def test_tripped_and_broken_are_errors_stale_is_a_warning():
    events = datadog.events_for(
        [_ok(kpi_id="forecast-slip-days", tripped=True), _stale(), _broken()],
        program_id="simulated-program",
    )
    by_title = {e["title"]: e["alert_type"] for e in events}
    assert by_title == {
        "forecast-slip-days tripped on simulated-program": "error",
        "weekly-spend-burn-ratio is stale on simulated-program": "warning",
        "blocked-share-pct is broken on simulated-program": "error",
    }


def test_a_tripped_reading_carries_its_detail():
    reading = Reading(
        kpi_id="cost-per-run-by-model", sim_date=DAY, value=0.381, tripped=True,
        detail="dearest is pr-review on claude-sonnet-4-6 at $0.381/run",
    )
    [event] = datadog.events_for([reading], program_id="eval-run-store")
    assert "dearest is pr-review" in event["text"]


# --- monitors --------------------------------------------------------------------------------


def _monitors(handle: str | None = None) -> dict[str, dict]:
    return {m["name"]: m for m in datadog.monitor_payloads("simulated-program", handle=handle)}


def test_three_monitors_per_program():
    assert set(_monitors()) == {
        "Program KPI tripped — simulated-program",
        "Program KPI unmeasured — simulated-program",
        "Program KPI heartbeat — simulated-program",
    }


def test_tripped_monitor_is_multi_alert_on_the_tripped_metric():
    m = _monitors()["Program KPI tripped — simulated-program"]
    assert m["query"] == (
        "max(last_1d):max:kpi.program.tripped{program:simulated-program} by {kpi} > 0"
    )
    assert m["options"]["notify_no_data"] is False


def test_unmeasured_monitor_warns_on_stale_and_alerts_on_broken():
    m = _monitors()["Program KPI unmeasured — simulated-program"]
    assert m["options"]["thresholds"] == {
        "critical": datadog.HEALTH["broken"],
        "warning": datadog.HEALTH["stale"],
    }
    assert datadog.HEALTH_METRIC in m["query"]


def test_heartbeat_threshold_can_never_fire_only_its_no_data_can():
    m = _monitors()["Program KPI heartbeat — simulated-program"]
    assert m["options"]["thresholds"]["critical"] > max(datadog.HEALTH.values())
    assert m["options"]["notify_no_data"] is True
    assert m["options"]["no_data_timeframe"] == datadog.NO_DATA_MINUTES


def test_handle_reaches_every_message_only_when_set():
    with_handle = _monitors(handle="@me@example.com")
    without = _monitors()
    assert all(m["message"].endswith("@me@example.com") for m in with_handle.values())
    assert not any("@me@example.com" in m["message"] for m in without.values())


# --- dashboards ------------------------------------------------------------------------------


@pytest.fixture()
def tree() -> dict[str, dict]:
    return {
        "cost-vs-envelope": {"id": "cost-vs-envelope", "name": "Cost vs envelope",
                             "unit": "ratio"},
        "forecast-slip-days": {"id": "forecast-slip-days", "name": "Forecast slip",
                               "unit": "days"},
    }


def test_dashboard_queries_every_shipping_kpi(tree):
    payload = datadog.dashboard_payload(
        "simulated-program", ["cost-vs-envelope", "forecast-slip-days"], tree
    )
    text = str(payload)
    assert "avg:kpi.program.cost_vs_envelope{program:simulated-program}" in text
    assert "avg:kpi.program.forecast_slip_days{program:simulated-program}" in text
    assert payload["title"] == "Program KPIs — simulated-program"


def test_dashboard_always_shows_health_and_tripped(tree):
    payload = datadog.dashboard_payload("simulated-program", ["cost-vs-envelope"], tree)
    titles = [w["definition"]["title"] for w in payload["widgets"]]
    assert "health (0 ok · 1 stale · 2 broken)" in titles
    assert "tripped thresholds" in titles


def test_dashboard_carries_the_event_stream(tree):
    payload = datadog.dashboard_payload("simulated-program", ["cost-vs-envelope"], tree)
    [stream] = [w for w in payload["widgets"] if w["definition"]["type"] == "event_stream"]
    assert stream["definition"]["query"] == "tags:generated:kpi-datadog,program:simulated-program"


def test_monitor_message_links_the_dashboard_only_when_resolved():
    with_url = datadog.monitor_payloads(
        "simulated-program", dashboard_url="https://app.datadoghq.com/dashboard/abc"
    )
    without = datadog.monitor_payloads("simulated-program")
    assert all("https://app.datadoghq.com/dashboard/abc" in m["message"] for m in with_url)
    assert not any("dashboard/abc" in m["message"] for m in without)
