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


@pytest.mark.parametrize("name", [*PROGRAM_IDS, "portfolio"])
def test_the_committed_files_match_the_generator(name, tmp_path):
    """The generator is the source of truth; a dashboard edited in Grafana and
    pasted back would silently stop matching what the stage ships."""
    dashboards.write_all(tmp_path, PROGRAM_IDS)
    fresh = json.loads((tmp_path / f"{name}.json").read_text())
    committed = json.loads((GRAFANA / f"{name}.json").read_text())
    assert fresh == committed, f"grafana/{name}.json is stale — rerun python -m kpi.dashboards"
