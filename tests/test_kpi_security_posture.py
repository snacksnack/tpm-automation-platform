"""Scanner alert counts → Datadog gauges (RC1-359) — offline, no network.

The GitHub side is exercised through an httpx MockTransport so pagination and
the 404-means-zero rule are real code paths, not assumptions. The payload
builder is pure and read directly, the way `test_kpi_datadog` reads
`series_for`. Shipping is `kpi.datadog.ship`, already covered there.
"""

from __future__ import annotations

import json

import httpx
import pytest

from kpi import security_posture as sp


def _alert(level: str | None) -> dict:
    rule = {"id": "py/path-injection"}
    if level is not None:
        rule["security_severity_level"] = level
    return {"state": "open", "rule": rule}


def _by_tags(series: list[dict]) -> dict[tuple[str, ...], dict]:
    return {tuple(s["tags"]): s for s in series}


# --- count_by_severity -----------------------------------------------------------------------


def test_all_four_severities_are_present_even_at_zero():
    counts = sp.count_by_severity([_alert("high"), _alert("high"), _alert("low")])
    assert counts == {"critical": 0, "high": 2, "medium": 0, "low": 1}


def test_an_alert_without_a_security_severity_lands_in_none():
    counts = sp.count_by_severity([_alert(None), _alert("medium")])
    assert counts["none"] == 1
    assert counts["medium"] == 1


def test_an_unknown_level_from_the_api_is_folded_into_none_not_copied():
    # Bounded tag set, and nothing from the response body reaches the log or
    # the payload — only our own labels and our counts do.
    counts = sp.count_by_severity([_alert("moderate"), _alert("HIGH")])
    assert counts["none"] == 2
    assert set(counts) == {*sp.SEVERITIES, "none"}


def test_summary_lines_use_a_fixed_severity_order():
    posture = {
        "r": {"code": sp.count_by_severity([_alert("low"), _alert("critical")]), "secret": 3}
    }
    (line,) = sp.summary_lines(posture)
    assert "critical 1, low 1" in line
    assert line.endswith("secret-scan open: 3")


def test_no_alerts_is_all_zeros():
    assert sp.count_by_severity([]) == {"critical": 0, "high": 0, "medium": 0, "low": 0}


# --- series_for ------------------------------------------------------------------------------


def test_each_repo_gets_four_code_series_and_one_secret_series():
    posture = {
        "reid_basic": {"code": {"critical": 0, "high": 7, "medium": 0, "low": 0}, "secret": 0},
        "pr_agent": {"code": {"critical": 0, "high": 0, "medium": 0, "low": 0}, "secret": 1},
    }
    series = sp.series_for(posture)
    assert len(series) == 10
    by_tags = _by_tags(series)
    high = by_tags[("repo:reid_basic", "severity:high")]
    assert high["metric"] == sp.CODE_METRIC
    assert high["type"] == sp.GAUGE
    assert high["points"][0]["value"] == 7.0
    assert isinstance(high["points"][0]["timestamp"], int)
    assert by_tags[("repo:reid_basic", "severity:critical")]["points"][0]["value"] == 0.0
    secret = by_tags[("repo:pr_agent",)]
    assert secret["metric"] == sp.SECRET_METRIC
    assert secret["points"][0]["value"] == 1.0


def test_the_none_bucket_is_emitted_only_when_populated():
    quiet = {
        "a": {"code": {"critical": 0, "high": 0, "medium": 0, "low": 0, "none": 0}, "secret": 0}
    }
    loud = {
        "a": {"code": {"critical": 0, "high": 0, "medium": 0, "low": 0, "none": 2}, "secret": 0}
    }
    assert ("repo:a", "severity:none") not in _by_tags(sp.series_for(quiet))
    none_series = _by_tags(sp.series_for(loud))[("repo:a", "severity:none")]
    assert none_series["points"][0]["value"] == 2.0


def test_the_series_payload_is_json_serializable():
    posture = {"a": {"code": sp.count_by_severity([_alert("critical")]), "secret": 0}}
    json.dumps({"series": sp.series_for(posture)})


# --- fetch_open_alerts (GitHub side, mocked transport) ---------------------------------------


def _client(handler) -> httpx.Client:
    return httpx.Client(base_url=sp.GITHUB_API, transport=httpx.MockTransport(handler))


def test_pagination_follows_the_link_header():
    seen: list[str] = []
    page_two = (
        f"{sp.GITHUB_API}/repos/snacksnack/x/code-scanning/alerts?state=open&per_page=100&page=2"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "page=2" in str(request.url):
            return httpx.Response(200, json=[_alert("low")])
        return httpx.Response(
            200,
            json=[_alert("high"), _alert("high")],
            headers={"Link": f'<{page_two}>; rel="next"'},
        )

    with _client(handler) as http:
        alerts = sp.fetch_open_alerts(http, "x", "code-scanning")
    assert len(alerts) == 3
    assert len(seen) == 2
    assert "state=open" in seen[0] and "per_page=100" in seen[0]


def test_a_404_is_zero_alerts_not_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "no analysis found"})

    with _client(handler) as http:
        assert sp.fetch_open_alerts(http, "x", "code-scanning") == []


def test_other_errors_still_raise():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Resource not accessible"})

    with _client(handler) as http, pytest.raises(httpx.HTTPStatusError) as exc_info:
        sp.fetch_open_alerts(http, "x", "secret-scanning")
    # A 403 must surface: it means the token lacks a scope or a repo.
    assert exc_info.value.response.status_code == 403


def test_collect_reads_both_kinds_for_every_repo():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "code-scanning" in path:
            return httpx.Response(200, json=[_alert("high")] if "reid_basic" in path else [])
        return httpx.Response(200, json=[{"state": "open"}] if "pr_agent" in path else [])

    with _client(handler) as http:
        posture = sp.collect(http, repos=("reid_basic", "pr_agent"))
    assert posture["reid_basic"]["code"]["high"] == 1
    assert posture["reid_basic"]["secret"] == 0
    assert posture["pr_agent"]["code"]["high"] == 0
    assert posture["pr_agent"]["secret"] == 1


# --- main ------------------------------------------------------------------------------------


def test_without_the_token_main_exits_2_and_says_which_secret(monkeypatch, capsys):
    monkeypatch.delenv(sp.TOKEN_ENV, raising=False)
    assert sp.main([]) == 2
    assert sp.TOKEN_ENV in capsys.readouterr().err


def _fixed_posture(*_args, **_kwargs) -> dict[str, dict]:
    return {"reid_basic": {"code": sp.count_by_severity([_alert("high")]), "secret": 0}}


def test_dry_run_prints_the_payload_and_ships_nothing(monkeypatch, capsys):
    monkeypatch.setenv(sp.TOKEN_ENV, "test-token")
    monkeypatch.setenv("DD_API_KEY", "would-be-used-if-shipping")
    monkeypatch.setattr(sp, "collect", _fixed_posture)
    monkeypatch.setattr(sp, "ship", lambda *a, **k: pytest.fail("dry run must not ship"))
    assert sp.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    payload = json.loads(out[out.index("{") : out.rindex("}") + 1])
    assert len(payload["series"]) == 5


def test_without_dd_api_key_main_exits_2_and_does_not_ship(monkeypatch, capsys):
    monkeypatch.setenv(sp.TOKEN_ENV, "test-token")
    monkeypatch.delenv("DD_API_KEY", raising=False)
    monkeypatch.setattr(sp, "collect", _fixed_posture)
    monkeypatch.setattr(sp, "ship", lambda *a, **k: pytest.fail("must not ship without a key"))
    assert sp.main([]) == 2
    assert "DD_API_KEY" in capsys.readouterr().err


def test_with_both_secrets_main_ships_once(monkeypatch):
    monkeypatch.setenv(sp.TOKEN_ENV, "test-token")
    monkeypatch.setenv("DD_API_KEY", "dd-key")
    monkeypatch.setattr(sp, "collect", _fixed_posture)
    shipped: list[tuple[list[dict], str]] = []
    monkeypatch.setattr(sp, "ship", lambda series, *, api_key: shipped.append((series, api_key)))
    assert sp.main([]) == 0
    assert len(shipped) == 1
    assert shipped[0][1] == "dd-key"
    assert len(shipped[0][0]) == 5
