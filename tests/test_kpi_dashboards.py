"""Generated Grafana dashboards (RC1-305).

The dashboards are generated so they cannot drift from the instrument stage's
verdict, so the test that matters is the one asserting that: every KPI the
stage verified appears on a chart, and nothing else does. The rest guards the
two properties that would quietly turn an honest chart into a lying one — a
null joined across as if it were a measurement, and a datasource uid baked in
from whichever instance the file was built on.

The queries themselves are verified by running them against Postgres, not
here; a test that asserts on SQL strings only proves the string is unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from collectors import programs
from kpi import dashboards, track

GRAFANA = Path(__file__).resolve().parent.parent / "grafana"
PROGRAM_IDS = sorted(programs.PROGRAMS)


@pytest.mark.parametrize("program_id", PROGRAM_IDS)
def test_every_verified_kpi_reaches_a_chart(program_id):
    """The sync guarantee: re-instrumenting and regenerating must not leave a
    KPI measured but invisible."""
    dash = dashboards.program_dashboard(program_id)
    charted = {
        kpi_id
        for panel in dash["panels"]
        if panel["type"] == "timeseries"
        for kpi_id in track.load_instrumentation(program_id).computes
        if f"'{kpi_id}'" in panel["targets"][0]["rawSql"]
    }
    assert charted == set(track.load_instrumentation(program_id).computes)


@pytest.mark.parametrize("program_id", PROGRAM_IDS)
def test_nothing_unverified_reaches_a_chart(program_id):
    dash = dashboards.program_dashboard(program_id)
    inst = track.load_instrumentation(program_id)
    not_shipping = {k.kpi_id for k in inst.kpis if k.status != "verified"}
    sql = " ".join(p["targets"][0]["rawSql"] for p in dash["panels"])
    for kpi_id in not_shipping:
        assert f"'{kpi_id}'" not in sql, f"{kpi_id} is not verified but is charted"


@pytest.mark.parametrize("program_id", PROGRAM_IDS)
def test_a_gap_is_never_drawn_as_a_line(program_id):
    """spanNulls would join across a day the KPI could not be measured, which
    is the chart equivalent of reporting a zero."""
    dash = dashboards.program_dashboard(program_id)
    for panel in dash["panels"]:
        if panel["type"] == "timeseries":
            assert panel["fieldConfig"]["defaults"]["custom"]["spanNulls"] is False


def test_dashboards_carry_no_instance_specific_datasource():
    """`__inputs` is what makes the file importable anywhere; a hard uid would
    import broken into a fresh Grafana Cloud org."""
    for dash in (*(dashboards.program_dashboard(p) for p in PROGRAM_IDS),
                 dashboards.portfolio_dashboard(PROGRAM_IDS)):
        assert dash["__inputs"][0]["name"] == "DS_POSTGRES"
        for panel in dash["panels"]:
            assert panel["datasource"]["uid"] == "${DS_POSTGRES}"


@pytest.mark.parametrize(
    ("prose", "expected"),
    [
        ("days", "d"),
        ("USD per scored case (and $ per full sweep)", "currencyUSD"),
        ("% of open points", "percent"),
        ("ratio, latest landed week", "none"),
        ("count", "short"),
        (None, "short"),
        ("something nobody mapped", "short"),
    ],
)
def test_prose_units_become_grafana_units(prose, expected):
    assert dashboards.grafana_unit(prose) == expected


def test_dollars_and_percents_do_not_share_an_axis():
    tree = {
        "a": {"unit": "USD per run"}, "b": {"unit": "% of cases"}, "c": {"unit": "USD total"},
    }
    groups = dashboards._group_by_unit(["a", "b", "c"], tree)
    assert groups == {"currencyUSD": ["a", "c"], "percent": ["b"]}


# --- push (RC1-318) --------------------------------------------------------------------------


_DS = [{"type": dashboards.DS_TYPE, "uid": "real-uid", "name": "reid-eval-store"}]


def _stack(datasources, posts):
    """A mock Grafana stack: answers the datasource question, records pushes."""
    def handler(request):
        if request.url.path == "/api/datasources":
            return httpx.Response(200, json=datasources)
        assert request.url.path == "/api/dashboards/db"
        body = json.loads(request.content)
        posts.append(body)
        uid = body["dashboard"]["uid"]
        return httpx.Response(
            200, json={"uid": uid, "url": f"/d/{uid}/x", "status": "success", "version": 2}
        )
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://stack.test")


def test_push_lands_every_dashboard_with_its_uid():
    """One push per dashboard, overwrite on, uid intact — the overwrite-in-place
    that keeps links alive is the whole point of the ticket."""
    posts: list[dict] = []
    with _stack(_DS, posts) as client:
        lines = dashboards.push_all(PROGRAM_IDS, client=client)
    assert {p["dashboard"]["uid"] for p in posts} == {
        *(f"kpi-{p}"[:40] for p in PROGRAM_IDS), "kpi-portfolio",
    }
    assert all(p["overwrite"] is True for p in posts)
    assert len(lines) == len(PROGRAM_IDS) + 1


def test_push_answers_the_datasource_prompt_itself():
    """What the import screen asks a human, the push path asks the stack: the
    placeholder is resolved to the real uid and the envelope keys are gone."""
    posts: list[dict] = []
    with _stack(_DS, posts) as client:
        dashboards.push_all(PROGRAM_IDS, client=client)
    for post in posts:
        dumped = json.dumps(post["dashboard"])
        assert "${DS_POSTGRES}" not in dumped
        assert '"real-uid"' in dumped
        assert "__inputs" not in post["dashboard"]
        assert "__requires" not in post["dashboard"]


def test_push_refuses_a_stack_without_the_datasource():
    with _stack([{"type": "loki", "uid": "x"}], []) as client:
        with pytest.raises(dashboards.PushError, match="no grafana-postgresql"):
            dashboards.push_all(PROGRAM_IDS, client=client)


def test_push_refuses_to_guess_between_two_datasources():
    two = [*_DS, {"type": dashboards.DS_TYPE, "uid": "other", "name": "second"}]
    with _stack(two, []) as client:
        with pytest.raises(dashboards.PushError, match="not guessing"):
            dashboards.push_all(PROGRAM_IDS, client=client)


def test_push_surfaces_a_refused_write():
    def handler(request):
        if request.url.path == "/api/datasources":
            return httpx.Response(200, json=_DS)
        return httpx.Response(403, json={"message": "insufficient permissions"})
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://stack.test")
    with client, pytest.raises(dashboards.PushError, match="HTTP 403"):
        dashboards.push_all(PROGRAM_IDS, client=client)


def test_push_without_a_token_stops_before_touching_anything(tmp_path, monkeypatch, capsys):
    """A half-done run (files rewritten, push silently skipped) would look like
    success; the command refuses up front instead."""
    monkeypatch.delenv("GRAFANA_TOKEN", raising=False)
    assert dashboards.main(["--out", str(tmp_path / "g"), "--push"]) == 2
    assert not (tmp_path / "g").exists()
    assert "GRAFANA_TOKEN" in capsys.readouterr().err


@pytest.mark.parametrize("name", [*PROGRAM_IDS, "portfolio"])
def test_the_committed_files_match_the_generator(name, tmp_path):
    """The generator is the source of truth; a dashboard edited in Grafana and
    pasted back would silently stop matching what the stage ships."""
    dashboards.write_all(tmp_path, PROGRAM_IDS)
    fresh = json.loads((tmp_path / f"{name}.json").read_text())
    committed = json.loads((GRAFANA / f"{name}.json").read_text())
    assert fresh == committed, f"grafana/{name}.json is stale — rerun python -m kpi.dashboards"
