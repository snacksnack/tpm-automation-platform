"""Fleet test-health counts (RC1-467) — offline, no network.

Same shape as `test_kpi_security_posture`: the Datadog side goes through an
httpx MockTransport, the series builder is pure and read directly, shipping
is `kpi.datadog.ship` and already covered there.
"""

from __future__ import annotations

import json

import httpx
import pytest

from kpi import test_health as th


def _aggregate_response(buckets: list[dict]) -> dict:
    return {"data": {"buckets": buckets}}


def _bucket(service: str, status: str, count: int) -> dict:
    return {
        "by": {"@test.service": service, "@test.status": status},
        "computes": {"c0": count},
    }


def _client(handler) -> httpx.Client:
    return httpx.Client(
        base_url="https://api.datadoghq.com", transport=httpx.MockTransport(handler)
    )


# --- fetch_status_counts ---------------------------------------------------------------------


def test_fetch_groups_by_service_and_status_and_filters_to_main():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=_aggregate_response(
                [_bucket("a", "pass", 40), _bucket("a", "fail", 2), _bucket("b", "pass", 7)]
            ),
        )

    with _client(handler) as http:
        counts = th.fetch_status_counts(http)
    assert counts == {"a": {"pass": 40, "fail": 2}, "b": {"pass": 7}}
    assert seen[0]["filter"]["query"] == th.BRANCH_QUERY
    assert {g["facet"] for g in seen[0]["group_by"]} == {"@test.service", "@test.status"}


def test_fetch_raises_on_an_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"errors": ["Forbidden"]})

    with _client(handler) as http, pytest.raises(httpx.HTTPStatusError):
        th.fetch_status_counts(http)


# --- series_for ------------------------------------------------------------------------------


def test_each_service_gets_attempted_and_passed_counts():
    series = th.series_for({"a": {"pass": 40, "fail": 2}})
    by_metric = {s["metric"]: s for s in series}
    assert by_metric[th.ATTEMPTED_METRIC]["points"][0]["value"] == 42.0
    assert by_metric[th.PASSED_METRIC]["points"][0]["value"] == 40.0
    for s in series:
        assert s["type"] == th.COUNT
        assert s["interval"] == th.DAY_S
        assert s["tags"] == [f"{th.SERVICE_TAG_KEY}:a"]


def test_skips_count_toward_neither_number():
    (attempted, passed) = th.series_for({"a": {"pass": 10, "skip": 5}})
    assert attempted["points"][0]["value"] == 10.0
    assert passed["points"][0]["value"] == 10.0


def test_a_service_with_only_skips_posts_nothing():
    assert th.series_for({"a": {"skip": 3}}) == []


def test_an_all_fail_day_still_posts_its_denominator():
    series = th.series_for({"a": {"fail": 4}})
    by_metric = {s["metric"]: s for s in series}
    assert by_metric[th.ATTEMPTED_METRIC]["points"][0]["value"] == 4.0
    assert by_metric[th.PASSED_METRIC]["points"][0]["value"] == 0.0


def test_the_series_payload_is_json_serializable():
    json.dumps({"series": th.series_for({"a": {"pass": 1, "fail": 1}})})


# --- main ------------------------------------------------------------------------------------


def test_without_both_keys_main_exits_2(monkeypatch, capsys):
    monkeypatch.setenv("DD_API_KEY", "k")
    monkeypatch.delenv("DD_APP_KEY", raising=False)
    assert th.main([]) == 2
    assert "DD_APP_KEY" in capsys.readouterr().err


def _fixed_counts(*_args, **_kwargs) -> dict[str, dict[str, int]]:
    return {"a": {"pass": 9, "fail": 1}}


def test_dry_run_prints_the_payload_and_ships_nothing(monkeypatch, capsys):
    monkeypatch.setenv("DD_API_KEY", "k")
    monkeypatch.setenv("DD_APP_KEY", "a")
    monkeypatch.setattr(th, "fetch_status_counts", _fixed_counts)
    monkeypatch.setattr(th, "ship", lambda *a, **k: pytest.fail("dry run must not ship"))
    assert th.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    payload = json.loads(out[out.index("{") : out.rindex("}") + 1])
    assert len(payload["series"]) == 2


def test_with_both_keys_main_ships_once(monkeypatch):
    monkeypatch.setenv("DD_API_KEY", "k")
    monkeypatch.setenv("DD_APP_KEY", "a")
    monkeypatch.setattr(th, "fetch_status_counts", _fixed_counts)
    shipped: list[list[dict]] = []
    monkeypatch.setattr(th, "ship", lambda series, *, api_key: shipped.append(series))
    assert th.main([]) == 0
    assert len(shipped) == 1


def test_a_quiet_day_posts_nothing_and_exits_0(monkeypatch, capsys):
    monkeypatch.setenv("DD_API_KEY", "k")
    monkeypatch.setenv("DD_APP_KEY", "a")
    monkeypatch.setattr(th, "fetch_status_counts", lambda *a, **k: {})
    monkeypatch.setattr(th, "ship", lambda *a, **k: pytest.fail("must not ship empty"))
    assert th.main([]) == 0
    assert "nothing to post" in capsys.readouterr().out
