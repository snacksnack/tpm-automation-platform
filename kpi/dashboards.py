"""Grafana dashboards, generated from the trees and the instrument reports (RC1-305).

Generated rather than hand-drawn, for the reason this epic exists: a
dashboard someone drew once goes stale the moment the instrument stage
changes its mind about a KPI, and a stale dashboard is a confident-looking
number nobody stands behind. These are rebuilt from
`docs/kpi/trees/<program>.adopted.json` (names, units, outcome vs leading)
and `docs/kpi/instruments/<program>.json` (what actually ships), so
re-instrumenting and regenerating keeps the picture honest.

    python -m kpi.dashboards --out grafana/

Import into Grafana Cloud (free tier) against the reid-eval-store Postgres;
the datasource is an `__inputs` placeholder, so import prompts for it rather
than carrying a uid from someone else's instance.

Two things the panels do on purpose:

- **Nulls are gaps, not zeros.** `spanNulls` is false everywhere. A day a KPI
  could not be measured leaves a hole in the line, which is what happened;
  joining across it would draw a trend through days nobody measured.
- **The unmeasured table is a panel, not a footnote.** Stale and broken
  readings are shown with their reason next to the charts, because "why is
  this flat" is the first question a chart like this provokes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from kpi.instrument import INSTRUMENTS, TREES
from kpi.track import load_instrumentation

DS_TYPE = "grafana-postgresql-datasource"
DS_REF = {"type": DS_TYPE, "uid": "${DS_POSTGRES}"}
TABLE = "kpi_readings"

#: The trees state units as prose ("% of open points", "USD per scored case")
#: because they are written for a reviewer, not for a charting library. This
#: is the only place that translates one into the other.
_UNITS = (
    ("usd", "currencyUSD"),
    ("%", "percent"),
    ("percent", "percent"),
    ("days", "d"),
    ("ratio", "none"),
    ("count", "short"),
)


def grafana_unit(unit: str | None) -> str:
    lowered = (unit or "").lower()
    for needle, grafana in _UNITS:
        if needle in lowered:
            return grafana
    return "short"


def load_tree(program_id: str, *, trees_dir: Path = TREES) -> dict[str, dict]:
    """Every KPI in the adopted tree, by id — outcomes and leading together."""
    data = json.loads((trees_dir / f"{program_id}.adopted.json").read_text())
    return {k["id"]: k for k in (*data["outcomes"], *data["leading"])}


# --- panels ----------------------------------------------------------------------------------


#: `reason` is a sentence, not a label — truncating it mid-word ("no spend row
#: has lan…") hides exactly the half that says why the KPI could not be read.
_TABLE_FIELDS = {
    "defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto", "wrapText": True}}},
    "overrides": [
        {
            "matcher": {"id": "byName", "options": "why not"},
            "properties": [{"id": "custom.width", "value": 420}],
        }
    ],
}


def _target(sql: str, *, fmt: str = "time_series") -> dict:
    return {
        "refId": "A",
        "datasource": DS_REF,
        "editorMode": "code",
        "format": fmt,
        "rawQuery": True,
        "rawSql": sql,
    }


def timeseries_panel(
    *, panel_id: int, title: str, description: str, unit: str, program_id: str,
    kpi_ids: list[str], grid: dict,
) -> dict:
    ids = ", ".join(f"'{k}'" for k in kpi_ids)
    sql = (
        f'SELECT sim_date::timestamptz AS "time", kpi_id AS metric, value\n'
        f"FROM {TABLE}\n"
        f"WHERE program_id = '{program_id}' AND kpi_id IN ({ids})\n"
        f"ORDER BY 1"
    )
    return {
        "id": panel_id,
        "type": "timeseries",
        "title": title,
        "description": description,
        "datasource": DS_REF,
        "gridPos": grid,
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {
                    "drawStyle": "line",
                    "lineWidth": 2,
                    "pointSize": 6,
                    "showPoints": "always",
                    # A day nobody could measure is a hole in the line, not a
                    # segment drawn through it.
                    "spanNulls": False,
                    "fillOpacity": 5,
                },
            },
            "overrides": [],
        },
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "none"},
        },
        "targets": [_target(sql)],
    }


def latest_table_panel(*, panel_id: int, program_id: str, grid: dict) -> dict:
    sql = (
        'SELECT r.kpi_id AS "KPI", r.state AS "state", r.value AS "value",\n'
        '       r.reason AS "why not", r.sim_date AS "day", r.run_id AS "snapshot run"\n'
        f"FROM {TABLE} r\n"
        "JOIN (SELECT kpi_id, max(sim_date) AS d\n"
        f"      FROM {TABLE} WHERE program_id = '{program_id}' GROUP BY kpi_id) latest\n"
        "  ON latest.kpi_id = r.kpi_id AND latest.d = r.sim_date\n"
        f"WHERE r.program_id = '{program_id}'\n"
        "ORDER BY r.state <> 'ok' DESC, r.kpi_id"
    )
    return {
        "id": panel_id,
        "type": "table",
        "title": "Latest reading per KPI",
        "description": (
            "The most recent reading for every shipping KPI. `state` is the honesty "
            "column: stale or broken carries the reason it could not be measured, and "
            "`snapshot run` is the run the number traces back to "
            "(`python -m collectors show <program> --run N`)."
        ),
        "datasource": DS_REF,
        "gridPos": grid,
        "fieldConfig": _TABLE_FIELDS,
        "options": {"showHeader": True, "sortBy": []},
        "targets": [_target(sql, fmt="table")],
    }


def unmeasured_stat_panel(*, panel_id: int, program_id: str, grid: dict) -> dict:
    sql = (
        'SELECT count(*) AS "unmeasured"\n'
        f"FROM {TABLE} r\n"
        "JOIN (SELECT kpi_id, max(sim_date) AS d\n"
        f"      FROM {TABLE} WHERE program_id = '{program_id}' GROUP BY kpi_id) latest\n"
        "  ON latest.kpi_id = r.kpi_id AND latest.d = r.sim_date\n"
        f"WHERE r.program_id = '{program_id}' AND r.state <> 'ok'"
    )
    return {
        "id": panel_id,
        "type": "stat",
        "title": "KPIs not measurable today",
        "description": "Stale or broken on the latest day. Anything above zero is RC1-307's.",
        "datasource": DS_REF,
        "gridPos": grid,
        "fieldConfig": {
            "defaults": {
                "unit": "short",
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"color": "green", "value": None},
                        {"color": "orange", "value": 1},
                    ],
                },
            },
            "overrides": [],
        },
        "options": {
            "colorMode": "background",
            "graphMode": "none",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
        "targets": [_target(sql, fmt="table")],
    }


# --- dashboards ------------------------------------------------------------------------------


def _group_by_unit(shipping: list[str], tree: dict[str, dict]) -> dict[str, list[str]]:
    """KPIs sharing a Grafana unit share an axis; mixing dollars and percents
    on one chart makes both unreadable."""
    groups: dict[str, list[str]] = {}
    for kpi_id in shipping:
        groups.setdefault(grafana_unit(tree.get(kpi_id, {}).get("unit")), []).append(kpi_id)
    return groups


_UNIT_TITLES = {
    "currencyUSD": "Cost",
    "percent": "Rates and shares",
    "d": "Days",
    "none": "Ratios",
    "short": "Counts",
}


def program_dashboard(program_id: str, *, trees_dir: Path = TREES,
                      instruments_dir: Path = INSTRUMENTS) -> dict:
    tree = load_tree(program_id, trees_dir=trees_dir)
    inst = load_instrumentation(program_id, instruments_dir=instruments_dir)
    shipping = inst.computes

    panels: list[dict] = [
        unmeasured_stat_panel(panel_id=1, program_id=program_id,
                              grid={"h": 8, "w": 6, "x": 0, "y": 0}),
        latest_table_panel(panel_id=2, program_id=program_id,
                           grid={"h": 8, "w": 18, "x": 6, "y": 0}),
    ]
    y, panel_id = 8, 3
    for unit, kpi_ids in _group_by_unit(shipping, tree).items():
        names = ", ".join(tree.get(k, {}).get("name", k) for k in kpi_ids)
        outcomes = [k for k in kpi_ids if tree.get(k, {}).get("type") == "outcome"]
        description = names + (
            f" — outcome KPI: {', '.join(outcomes)}." if outcomes else " — leading indicators."
        )
        panels.append(
            timeseries_panel(
                panel_id=panel_id, title=_UNIT_TITLES.get(unit, unit), description=description,
                unit=unit, program_id=program_id, kpi_ids=kpi_ids,
                grid={"h": 8, "w": 12, "x": 0 if panel_id % 2 else 12, "y": y},
            )
        )
        panel_id += 1
        if panel_id % 2:
            y += 8

    return _wrap(
        title=f"KPI — {program_id}",
        uid=f"kpi-{program_id}"[:40],
        description=(
            f"Readings for {program_id}, computed by code from dated snapshots (RC1-305). "
            "Gaps are days the KPI could not be measured; they are never drawn as zero."
        ),
        panels=panels,
        tags=["kpi", program_id],
    )


def portfolio_dashboard(program_ids: list[str]) -> dict:
    quoted = ", ".join(f"'{p}'" for p in program_ids)
    latest_sql = (
        'SELECT r.program_id AS "program", r.kpi_id AS "KPI", r.state AS "state",\n'
        '       r.value AS "value", r.reason AS "why not", r.sim_date AS "day"\n'
        f"FROM {TABLE} r\n"
        "JOIN (SELECT program_id, kpi_id, max(sim_date) AS d\n"
        f"      FROM {TABLE} WHERE program_id IN ({quoted})\n"
        "      GROUP BY program_id, kpi_id) latest\n"
        "  ON latest.program_id = r.program_id AND latest.kpi_id = r.kpi_id\n"
        " AND latest.d = r.sim_date\n"
        f"WHERE r.program_id IN ({quoted})\n"
        "ORDER BY r.state <> 'ok' DESC, r.program_id, r.kpi_id"
    )
    tripped_sql = (
        'SELECT count(*) AS "tripped"\n'
        f"FROM {TABLE} r\n"
        "JOIN (SELECT program_id, kpi_id, max(sim_date) AS d\n"
        f"      FROM {TABLE} GROUP BY program_id, kpi_id) latest\n"
        "  ON latest.program_id = r.program_id AND latest.kpi_id = r.kpi_id\n"
        " AND latest.d = r.sim_date\n"
        "WHERE r.tripped"
    )
    coverage_sql = (
        'SELECT sim_date::timestamptz AS "time", program_id AS metric, count(*) AS value\n'
        f"FROM {TABLE}\n"
        "WHERE state = 'ok'\n"
        "GROUP BY 1, 2 ORDER BY 1"
    )

    panels = [
        {
            "id": 1, "type": "stat", "title": "Thresholds crossed",
            "description": "Latest reading per KPI, across every program, with its so-what "
                           "threshold live.",
            "datasource": DS_REF, "gridPos": {"h": 12, "w": 6, "x": 0, "y": 0},
            "fieldConfig": {
                "defaults": {
                    "unit": "short",
                    "thresholds": {"mode": "absolute", "steps": [
                        {"color": "green", "value": None}, {"color": "red", "value": 1}]},
                },
                "overrides": [],
            },
            "options": {
                "colorMode": "background", "graphMode": "none",
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            },
            "targets": [_target(tripped_sql, fmt="table")],
        },
        latest_table_panel_all(panel_id=2, sql=latest_sql,
                               grid={"h": 12, "w": 18, "x": 6, "y": 0}),
        {
            "id": 3, "type": "timeseries", "title": "KPIs measured per day",
            "description": "How many KPIs each program could actually read. A step down is a "
                           "source that broke, not a program that improved.",
            "datasource": DS_REF, "gridPos": {"h": 8, "w": 24, "x": 0, "y": 12},
            "fieldConfig": {
                "defaults": {"unit": "short", "custom": {
                    "drawStyle": "line", "lineWidth": 2, "pointSize": 6,
                    "showPoints": "always", "spanNulls": False, "fillOpacity": 5}},
                "overrides": [],
            },
            "options": {
                "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                "tooltip": {"mode": "multi", "sort": "none"},
            },
            "targets": [_target(coverage_sql)],
        },
    ]
    return _wrap(
        title="KPI — portfolio",
        uid="kpi-portfolio",
        description="Every program's KPIs on one page (RC1-305; the rollup RC1-233 consumes).",
        panels=panels,
        tags=["kpi", "portfolio"],
    )


def latest_table_panel_all(*, panel_id: int, sql: str, grid: dict) -> dict:
    return {
        "id": panel_id,
        "type": "table",
        "title": "Latest reading per KPI, every program",
        "description": "Unmeasurable KPIs sort to the top, with the reason.",
        "datasource": DS_REF,
        "gridPos": grid,
        "fieldConfig": _TABLE_FIELDS,
        "options": {"showHeader": True, "sortBy": []},
        "targets": [_target(sql, fmt="table")],
    }


def _wrap(*, title: str, uid: str, description: str, panels: list[dict], tags: list[str]) -> dict:
    """The export envelope. `__inputs` is what makes the file importable into
    any instance: Grafana prompts for the Postgres datasource instead of the
    dashboard carrying a uid from wherever it was built."""
    return {
        "__inputs": [
            {
                "name": "DS_POSTGRES",
                "label": "Postgres (reid-eval-store)",
                "description": "The database the readings land in — see docs/kpi/metrics-store.md",
                "type": "datasource",
                "pluginId": DS_TYPE,
                "pluginName": "PostgreSQL",
            }
        ],
        "__requires": [
            {"type": "datasource", "id": DS_TYPE, "name": "PostgreSQL", "version": "1.0.0"},
            {"type": "grafana", "id": "grafana", "name": "Grafana", "version": "10.0.0"},
        ],
        "title": title,
        "uid": uid,
        "description": description,
        "tags": tags,
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "refresh": "",
        # The simulated program's sim-dates run about two weeks ahead of the
        # wall clock (kickoff 2026-09-07 was seeded on 2026-08-23), so the
        # window has to reach into the future or its charts open empty. It also
        # has to stay tight: at a multi-year zoom a fortnight of daily readings
        # collapses into a couple of specks. Thirty days back covers the three
        # weeks of history RC1-306 needs; forty-five forward covers the sim
        # clock through GA.
        "time": {"from": "now-30d", "to": "now+45d"},
        "panels": panels,
        "annotations": {"list": []},
        "templating": {"list": []},
    }


# --- CLI -------------------------------------------------------------------------------------


def write_all(out_dir: Path, program_ids: list[str]) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for program_id in program_ids:
        path = out_dir / f"{program_id}.json"
        path.write_text(json.dumps(program_dashboard(program_id), indent=2) + "\n")
        written.append(path)
    path = out_dir / "portfolio.json"
    path.write_text(json.dumps(portfolio_dashboard(program_ids), indent=2) + "\n")
    written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    from collectors import programs

    ap = argparse.ArgumentParser(
        prog="python -m kpi.dashboards",
        description="Generate the Grafana dashboards from the trees and instrument reports.",
    )
    ap.add_argument("--out", type=Path, default=Path("grafana"), help="directory to write into")
    args = ap.parse_args(argv)

    for path in write_all(args.out, sorted(programs.PROGRAMS)):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
