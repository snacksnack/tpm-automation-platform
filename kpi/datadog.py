"""The Datadog leg of the track stage (RC1-305, revisited 2026-08-29).

The original RC1-304 decision sent the readings to Postgres because the
Datadog free plan forgets a metric within a day (`docs/kpi/metrics-store.md`).
The account is on the Pro plan now — fifteen months of retention — so the
premise flipped, and this module ships each tracked day to Datadog as custom
metrics *in addition to* the Postgres rows. Dual-write, not a migration:
Postgres stays the system of record that the briefs (RC1-306), the escalate
stage (RC1-307), Grafana (RC1-318) and the dead-man's switch (RC1-319) all
read; Datadog is the public picture. A Datadog outage therefore costs the
picture a point, never the record — `ship_result` failures are reported and
swallowed by the caller, and the day's exit code belongs to Postgres alone.

The honesty rule crosses over intact:

- **A value point exists only for an `ok` reading.** Stale and broken days
  leave a gap in the series — Datadog draws holes for missing points the way
  the Grafana panels keep `spanNulls` off. Never a zero for unknown.
- **The state always ships**, as `kpi.program.health` (0 ok, 1 stale,
  2 broken) tagged with the KPI id, so a monitor can page on "unmeasured"
  even while the value series goes quiet.
- **`kpi.program.tripped`** (0/1) ships per KPI, which is what lets the
  planted events — the day-29 slip, the week-6 cost spike — fire real
  Datadog monitors when they land.

Tags are `program:` and (on the shared metrics) `kpi:` only. The sim-date is
deliberately not a tag: every distinct tag combination is a billable custom
metric, and a date tag would multiply the count by the length of the program
for no chart the wall-clock timeline doesn't already draw — the daily job
ticks one sim-day per real day.

`DD_API_KEY` lives in `~/.zshrc` next to `EVAL_DATABASE_URL` and is pulled
into the launchd environment by `scripts/kpi_daily.sh` the same way. Absent,
the shipper skips with a note and the run is otherwise unchanged — the leg
is additive. `DD_SITE` overrides the region (default `datadoghq.com`, US1,
where the account lives). Dashboards are pushed with `DD_APP_KEY` via
`python -m kpi.datadog dashboards --push`, which builds them from the same
adopted trees and instrument reports the Grafana generator reads, upserting
by title so the URLs survive regeneration. `monitors --push` lands the
alerting on those metrics the same way — tripped, unmeasured, and a
no-data heartbeat per program — with the page destination taken from
`DD_ALERT_HANDLE` at push time. `slos --push` (RC1-324) puts error budgets
on top: per program, the share of time the unmeasured / heartbeat / tripped
monitors sat in OK, plus one fleet-wide objective on the RC1-322 trace
metrics — push order is monitors, then slos, then dashboards (the dashboard
gains an SLO row once the SLOs exist). `docs/kpi/slos.md` has the layering
argument and the targets.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

from kpi.reading import Reading

VALUE_METRIC_PREFIX = "kpi.program."
HEALTH_METRIC = "kpi.program.health"
TRIPPED_METRIC = "kpi.program.tripped"

HEALTH = {"ok": 0, "stale": 1, "broken": 2}

#: v2 series `type`: 3 is gauge — each point stands alone, no rate math.
GAUGE = 3


def api_url(path: str, *, site: str | None = None) -> str:
    site = site or os.environ.get("DD_SITE", "datadoghq.com")
    return f"https://api.{site}{path}"


def metric_name(kpi_id: str) -> str:
    """`forecast-slip-days` → `kpi.program.forecast_slip_days`. Datadog folds
    hyphens to underscores on ingest; folding here keeps the name we query
    identical to the name we sent."""
    return VALUE_METRIC_PREFIX + kpi_id.replace("-", "_")


def _series(metric: str, value: float, *, at: int, tags: list[str]) -> dict:
    return {
        "metric": metric,
        "type": GAUGE,
        "points": [{"timestamp": at, "value": value}],
        "tags": tags,
    }


def series_for(readings: list[Reading], *, program_id: str, at: int) -> list[dict]:
    """The v2 series payload for one tracked day. Pure — the tests read this
    rather than a network.

    `at` is wall-clock submit time, not the sim-date: Datadog refuses points
    much older than an hour, and the daily job's one-tick-per-day cadence
    makes the real timeline the sim timeline drawn at 1:1.
    """
    out: list[dict] = []
    program_tag = f"program:{program_id}"
    for r in readings:
        kpi_tag = f"kpi:{r.kpi_id}"
        if r.state == "ok" and r.value is not None:
            out.append(_series(metric_name(r.kpi_id), r.value, at=at, tags=[program_tag]))
        out.append(
            _series(HEALTH_METRIC, HEALTH[r.state], at=at, tags=[program_tag, kpi_tag])
        )
        out.append(
            _series(TRIPPED_METRIC, float(r.tripped), at=at, tags=[program_tag, kpi_tag])
        )
    return out


def events_for(readings: list[Reading], *, program_id: str) -> list[dict]:
    """One event per reading with something to explain (RC1-329) — tripped, or
    a state that is not ok. This is how the *why* crosses the wire: metrics
    carry only numbers and tags, so the reading's reason and detail — the text
    a person needs when the monitor fires — ride the Events API instead and
    surface in the dashboards' event stream. Pure.

    `aggregation_key` is per (program, kpi): a KPI that stays tripped for a
    week rolls up in the stream rather than posting seven look-alike rows.
    """
    out: list[dict] = []
    for r in readings:
        if not r.tripped and r.state == "ok":
            continue
        if r.tripped:
            title = f"{r.kpi_id} tripped on {program_id}"
        else:
            title = f"{r.kpi_id} is {r.state} on {program_id}"
        body = "\n\n".join(part for part in (r.reason, r.detail) if part)
        out.append(
            {
                "title": title,
                "text": body or "(no detail recorded on the reading)",
                "alert_type": "warning" if (r.state == "stale" and not r.tripped) else "error",
                "aggregation_key": f"kpi-reading:{program_id}:{r.kpi_id}",
                "tags": [
                    f"program:{program_id}",
                    f"kpi:{r.kpi_id}",
                    "kind:kpi-reading",  # the event-stream widgets filter on this
                    "generated:kpi-datadog",
                ],
            }
        )
    return out


def ship(series: list[dict], *, api_key: str, site: str | None = None) -> None:
    resp = httpx.post(
        api_url("/api/v2/series", site=site),
        json={"series": series},
        headers={"DD-API-KEY": api_key},
        timeout=30,
    )
    resp.raise_for_status()


def ship_events(events: list[dict], *, api_key: str, site: str | None = None) -> None:
    for event in events:  # the v1 events endpoint takes one event per call
        resp = httpx.post(
            api_url("/api/v1/events", site=site),
            json=event,
            headers={"DD-API-KEY": api_key},
            timeout=30,
        )
        resp.raise_for_status()


def ship_readings(
    readings: list[Reading], *, program_id: str, at: int | None = None
) -> tuple[int, int] | None:
    """Ship one tracked day: the numbers as series, the why as events.
    Returns (series, events) counts, or None when `DD_API_KEY` is unset
    (the leg is optional; Postgres is not)."""
    api_key = os.environ.get("DD_API_KEY")
    if not api_key:
        return None
    series = series_for(readings, program_id=program_id, at=at or int(time.time()))
    events = events_for(readings, program_id=program_id)
    if series:
        ship(series, api_key=api_key)
    if events:
        ship_events(events, api_key=api_key)
    return len(series), len(events)


# --- monitors --------------------------------------------------------------------------------

#: Baked into the monitor message at push time, not read at alert time —
#: e.g. `export DD_ALERT_HANDLE="@you@example.com"` in ~/.zshrc pages that
#: address; unset, the monitors alert in the UI and event stream only.
ALERT_HANDLE_ENV = "DD_ALERT_HANDLE"

#: The heartbeat threshold can never fire — health tops out at 2 — so the
#: monitor's only job is its no-data state: the daily job runs at 07:00, and
#: 26 hours of silence means the job or the shipper died, not a quiet day.
HEARTBEAT_THRESHOLD = 99
NO_DATA_MINUTES = 26 * 60


def _message(lead: str, *, handle: str | None, dashboard_url: str | None = None) -> str:
    body = (
        f"{lead}\n\n"
        "Numbers are computed by `kpi.track` from snapshots and shipped by "
        "`kpi/datadog.py` (RC1-305); the reading's reason lives in Postgres — "
        "`python -m collectors show <program> --run <id>` traces it."
    )
    if dashboard_url:
        body += (
            f"\n\nWhy: the event stream on [the program dashboard]({dashboard_url}) "
            "carries this reading's reason and detail (RC1-329)."
        )
    return f"{body}\n\n{handle}" if handle else body


def monitor_payloads(
    program_id: str, *, handle: str | None = None, dashboard_url: str | None = None
) -> list[dict]:
    """Three monitors per program, generated so they cannot drift from the
    metrics the shipper actually sends. Pure.

    - **tripped**: a KPI's so-what threshold crossed — the planted sim events
      land here. Multi-alert by `kpi` so each names itself.
    - **unmeasured**: warning on stale (1), alert on broken (2) — the honesty
      rule's alarm. The value chart goes quiet and this says why.
    - **heartbeat**: a threshold that never fires plus `notify_no_data` — the
      Datadog-side twin of the dead-man's switch (RC1-319).
    """
    scope = f"{{program:{program_id}}}"
    tags = [f"program:{program_id}", "generated:kpi-datadog"]
    return [
        {
            "name": f"Program KPI tripped — {program_id}",
            "type": "metric alert",
            "query": f"max(last_1d):max:{TRIPPED_METRIC}{scope} by {{kpi}} > 0",
            "message": _message(
                f"{{{{kpi.name}}}} tripped its threshold on {program_id}.",
                handle=handle,
                dashboard_url=dashboard_url,
            ),
            "tags": tags,
            "options": {"thresholds": {"critical": 0}, "notify_no_data": False},
        },
        {
            "name": f"Program KPI unmeasured — {program_id}",
            "type": "metric alert",
            "query": f"max(last_1d):max:{HEALTH_METRIC}{scope} by {{kpi}} >= 2",
            "message": _message(
                f"{{{{kpi.name}}}} on {program_id} is unmeasured — "
                "stale on warning, broken on alert. Never mistaken for a zero.",
                handle=handle,
                dashboard_url=dashboard_url,
            ),
            "tags": tags,
            "options": {
                "thresholds": {"critical": 2, "warning": 1},
                "notify_no_data": False,
            },
        },
        {
            "name": f"Program KPI heartbeat — {program_id}",
            "type": "metric alert",
            "query": f"max(last_1d):max:{HEALTH_METRIC}{scope} > {HEARTBEAT_THRESHOLD}",
            "message": _message(
                f"No KPI readings have reached Datadog from {program_id} for "
                "26+ hours: the daily job or the shipper is down.",
                handle=handle,
                dashboard_url=dashboard_url,
            ),
            "tags": tags,
            "options": {
                "thresholds": {"critical": HEARTBEAT_THRESHOLD},
                "notify_no_data": True,
                "no_data_timeframe": NO_DATA_MINUTES,
            },
        },
    ]


def push_monitors(program_ids: list[str]) -> list[str]:
    """Create or update the monitors, matched by name — same contract as the
    dashboards, so a regenerate never spawns duplicates."""
    handle = os.environ.get(ALERT_HANDLE_ENV)
    lines: list[str] = []
    with _client() as client:
        existing = {
            m["name"]: m["id"]
            for m in client.get("/api/v1/monitor", params={"page_size": 200}).json()
        }
        dashboards = {
            d["title"]: d["url"]
            for d in client.get("/api/v1/dashboard").json().get("dashboards", [])
        }
        for program_id in program_ids:
            dash_path = dashboards.get(f"Program KPIs — {program_id}")
            dash_url = f"https://app.datadoghq.com{dash_path}" if dash_path else None
            for payload in monitor_payloads(program_id, handle=handle, dashboard_url=dash_url):
                monitor_id = existing.get(payload["name"])
                if monitor_id:
                    resp = client.put(f"/api/v1/monitor/{monitor_id}", json=payload)
                else:
                    resp = client.post("/api/v1/monitor", json=payload)
                resp.raise_for_status()
                data = resp.json()
                lines.append(f"{data['id']}  {data['name']}")
    if not handle:
        lines.append(
            f"note: {ALERT_HANDLE_ENV} is unset — monitors alert in the UI only, "
            "no one is paged"
        )
    return lines


# --- SLOs (RC1-324) --------------------------------------------------------------------------

#: 30-day rolling window on every objective — long enough that one bad day
#: cannot dominate, short enough that last month's sins age out.
SLO_WINDOW = "30d"

#: Targets are provisional until two weeks of soak calibrates them against the
#: observed burn (a target should come from a baseline, not a wish).
#: Measurement layer at 99 %: ~7.2 h/month of stale/broken/silent tolerated.
#: Program layer at 95 %: a program is *allowed* to run red 5 % of the time —
#: the budget is what separates normal red from escalation-worthy red.
MEASUREMENT_TARGET = 99.0
LIVENESS_TARGET = 99.0
PROGRAM_TARGET = 95.0
FLEET_TARGET = 97.0

#: SLO name -> the generated monitor (by its name lead) that carries the SLI.
_SLO_ON_MONITOR = [
    ("Program measurement health", "Program KPI unmeasured", MEASUREMENT_TARGET),
    ("Program pipeline liveness", "Program KPI heartbeat", LIVENESS_TARGET),
    ("Program health", "Program KPI tripped", PROGRAM_TARGET),
]

FLEET_SLO_NAME = "Agent fleet reliability"


def slo_payloads(program_id: str, *, monitor_ids: dict[str, int]) -> list[dict]:
    """Three monitor-based SLOs per program, layered so the budgets never mix:
    the measurement layer (unmeasured, heartbeat) says whether the numbers can
    be trusted; the program layer (tripped) says how much red is tolerable.
    A dead collector must burn *its* budget, not hide inside the program's.

    Pure: `monitor_ids` maps the monitor name lead (e.g. "Program KPI
    tripped") to the id the caller resolved — the SLI is the fraction of time
    that monitor sat in OK.
    """
    tags = [f"program:{program_id}", "generated:kpi-datadog"]
    payloads = []
    for slo_lead, monitor_lead, target in _SLO_ON_MONITOR:
        payloads.append(
            {
                "name": f"{slo_lead} — {program_id}",
                "type": "monitor",
                "monitor_ids": [monitor_ids[monitor_lead]],
                "thresholds": [{"timeframe": SLO_WINDOW, "target": target}],
                "description": (
                    f"Fraction of time '{monitor_lead} — {program_id}' sat in OK "
                    f"over {SLO_WINDOW}. Generated by kpi/datadog.py (RC1-324); "
                    "targets are provisional until soak calibrates them."
                ),
                "tags": tags,
            }
        )
    return payloads


def fleet_slo_payload() -> dict:
    """One metric-based SLO over the whole agent fleet: share of model calls
    that did not error, from the APM trace metrics LLM Observability's spans
    generate (RC1-322). `default_zero` because `.errors` only materialises
    after the first error — a fleet that has never failed must read 100 %,
    not no-data."""
    hits = "sum:trace.anthropic.request.hits{*}.as_count()"
    errors = "default_zero(sum:trace.anthropic.request.errors{*}.as_count())"
    return {
        "name": FLEET_SLO_NAME,
        "type": "metric",
        "query": {"numerator": f"{hits} - {errors}", "denominator": hits},
        "thresholds": [{"timeframe": SLO_WINDOW, "target": FLEET_TARGET}],
        "description": (
            "Share of the agent fleet's model calls that completed without "
            "error, across every ml_app (RC1-322 traces). Generated by "
            "kpi/datadog.py (RC1-324)."
        ),
        "tags": ["generated:kpi-datadog", "fleet:agents"],
    }


def push_slos(program_ids: list[str]) -> list[str]:
    """Create or update the SLOs, matched by name — the same contract as the
    monitors and dashboards. Monitor-based objectives need the monitors to
    exist first: run `monitors --push` before the first `slos --push`."""
    lines: list[str] = []
    with _client() as client:
        monitors = {
            m["name"]: m["id"]
            for m in client.get("/api/v1/monitor", params={"page_size": 200}).json()
        }
        existing = {
            s["name"]: s["id"]
            for s in client.get("/api/v1/slo", params={"limit": 1000}).json().get("data", [])
        }

        def upsert(payload: dict) -> None:
            slo_id = existing.get(payload["name"])
            if slo_id:
                resp = client.put(f"/api/v1/slo/{slo_id}", json=payload)
            else:
                resp = client.post("/api/v1/slo", json=payload)
            resp.raise_for_status()
            data = resp.json()["data"][0]
            lines.append(f"{data['id']}  {data['name']}")

        for program_id in program_ids:
            monitor_ids = {}
            for _, monitor_lead, _ in _SLO_ON_MONITOR:
                name = f"{monitor_lead} — {program_id}"
                if name not in monitors:
                    raise SystemExit(
                        f"monitor {name!r} not found — run "
                        "`python -m kpi.datadog monitors --push` first"
                    )
                monitor_ids[monitor_lead] = monitors[name]
            for payload in slo_payloads(program_id, monitor_ids=monitor_ids):
                upsert(payload)
        upsert(fleet_slo_payload())
    return lines


# --- dashboards ------------------------------------------------------------------------------


def _timeseries_widget(title: str, queries: list[tuple[str, str]]) -> dict:
    return {
        "definition": {
            "type": "timeseries",
            "title": title,
            "requests": [
                {
                    "queries": [
                        {"name": f"q{i}", "data_source": "metrics", "query": q}
                        for i, (q, _) in enumerate(queries)
                    ],
                    "formulas": [
                        {"formula": f"q{i}", "alias": alias}
                        for i, (_, alias) in enumerate(queries)
                    ],
                    "response_format": "timeseries",
                    "display_type": "line",
                }
            ],
        }
    }


def dashboard_payload(
    program_id: str,
    shipping: list[str],
    tree: dict[str, dict],
    slo_ids: dict[str, str] | None = None,
) -> dict:
    """One program's dashboard, built from the same adopted tree and
    instrument verdicts the Grafana generator reads — regenerating after a
    re-instrument keeps this picture honest too. Pure.

    `slo_ids` maps this program's SLO names to Datadog SLO ids (RC1-324);
    the caller resolves them so the first dashboards push — before any SLO
    exists — still works, just without the budget row.
    """
    from kpi.dashboards import _group_by_unit  # same grouping as the Grafana panels

    scope = f"{{program:{program_id}}}"
    widgets = []
    for slo_lead, _, _ in _SLO_ON_MONITOR:
        slo_id = (slo_ids or {}).get(f"{slo_lead} — {program_id}")
        if slo_id:
            widgets.append(
                {
                    "definition": {
                        "type": "slo",
                        "slo_id": slo_id,
                        "title": slo_lead,
                        "view_type": "detail",
                        "time_windows": [SLO_WINDOW],
                        "show_error_budget": True,
                        "view_mode": "overall",
                    }
                }
            )
    for unit, kpi_ids in _group_by_unit(shipping, tree).items():
        queries = [
            (f"avg:{metric_name(k)}{scope}", tree.get(k, {}).get("name", k))
            for k in kpi_ids
        ]
        widgets.append(_timeseries_widget(unit or "value", queries))
    widgets.append(
        _timeseries_widget(
            "health (0 ok · 1 stale · 2 broken)",
            [(f"avg:{HEALTH_METRIC}{scope} by {{kpi}}", "health")],
        )
    )
    widgets.append(
        _timeseries_widget(
            "tripped thresholds",
            [(f"max:{TRIPPED_METRIC}{scope} by {{kpi}}", "tripped")],
        )
    )
    # The why (RC1-329): every tripped/unmeasured reading posts its reason and
    # detail as an event, so the red on the charts explains itself in place.
    widgets.append(
        {
            "definition": {
                "type": "event_stream",
                "title": "why — the tripped and unmeasured readings, in their own words",
                # Plain facet syntax (space = AND) — the events platform cannot parse
                # the legacy "tags:a,b" form (RC1-330). kind: is ours alone, so the
                # monitors' own audit/trigger events stay out of the stream.
                "query": f"kind:kpi-reading program:{program_id}",
                "event_size": "l",
            }
        }
    )
    # The what-changed (RC1-334): PRs and pushes from the GitHub App, so a
    # reading that moved and the merge that moved it sit on the same page.
    # Unscoped by program — GitHub events are repo-shaped, and the platform
    # repo's merges change what every reading means; narrow to repo tags
    # once real events show which facets they carry.
    widgets.append(
        {
            "definition": {
                "type": "event_stream",
                "title": "delivery — GitHub PRs and pushes",
                "query": "source:github",
                "event_size": "s",
            }
        }
    )
    return {
        "title": f"Program KPIs — {program_id}",
        "description": (
            "Generated by python -m kpi.datadog dashboards (RC1-305). "
            "Values ship only for ok readings — gaps are unmeasured days, never zeros; "
            "the health series says why."
        ),
        "layout_type": "ordered",
        "widgets": widgets,
    }


def _client() -> httpx.Client:
    api_key = os.environ.get("DD_API_KEY")
    app_key = os.environ.get("DD_APP_KEY")
    if not api_key or not app_key:
        raise SystemExit(
            "DD_API_KEY and DD_APP_KEY are required to push dashboards "
            "(they live in ~/.zshrc)"
        )
    return httpx.Client(
        base_url=api_url(""),
        headers={"DD-API-KEY": api_key, "DD-APPLICATION-KEY": app_key},
        timeout=30,
    )


def push_dashboards(program_ids: list[str]) -> list[str]:
    """Create or update one dashboard per program, matched by title so the
    URL — and every link to it, shared ones included — survives a
    regeneration."""
    from kpi.dashboards import load_tree
    from kpi.track import load_instrumentation

    urls: list[str] = []
    with _client() as client:
        existing = {
            d["title"]: d["id"]
            for d in client.get("/api/v1/dashboard").json().get("dashboards", [])
        }
        slo_ids = {
            s["name"]: s["id"]
            for s in client.get("/api/v1/slo", params={"limit": 1000}).json().get("data", [])
        }
        for program_id in program_ids:
            tree = load_tree(program_id)
            shipping = load_instrumentation(program_id).computes
            payload = dashboard_payload(program_id, shipping, tree, slo_ids)
            dash_id = existing.get(payload["title"])
            if dash_id:
                resp = client.put(f"/api/v1/dashboard/{dash_id}", json=payload)
            else:
                resp = client.post("/api/v1/dashboard", json=payload)
            resp.raise_for_status()
            urls.append(resp.json()["url"])
    return urls


def main(argv: list[str] | None = None) -> int:
    from collectors import programs

    ap = argparse.ArgumentParser(
        prog="python -m kpi.datadog",
        description="Datadog leg of the track stage: dashboards from the adopted trees.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    dash = sub.add_parser("dashboards", help="build the per-program dashboards")
    dash.add_argument("--push", action="store_true", help="create/update them via the API")
    mon = sub.add_parser("monitors", help="build the per-program monitors")
    mon.add_argument("--push", action="store_true", help="create/update them via the API")
    slo = sub.add_parser("slos", help="build the program-health SLOs (RC1-324)")
    slo.add_argument("--push", action="store_true", help="create/update them via the API")
    args = ap.parse_args(argv)

    ids = sorted(programs.PROGRAMS)
    if args.cmd == "slos":
        if args.push:
            for line in push_slos(ids):
                print(line)
        else:
            for program_id in ids:
                for slo_lead, monitor_lead, target in _SLO_ON_MONITOR:
                    print(
                        f"{slo_lead} — {program_id}\n"
                        f"  {target}% of {SLO_WINDOW} with "
                        f"'{monitor_lead} — {program_id}' in OK"
                    )
            print(
                f"{FLEET_SLO_NAME}\n  {FLEET_TARGET}% of {SLO_WINDOW}: "
                "model calls without error, fleet-wide"
            )
            print("(use --push to land; monitors must be pushed first)")
        return 0
    if args.cmd == "monitors":
        if args.push:
            for line in push_monitors(ids):
                print(line)
        else:
            handle = os.environ.get(ALERT_HANDLE_ENV)
            for program_id in ids:
                for payload in monitor_payloads(program_id, handle=handle):
                    print(f"{payload['name']}\n  {payload['query']}")
            print("(use --push to land)")
        return 0
    if args.push:
        for url in push_dashboards(ids):
            print(url)
    else:
        import json

        from kpi.dashboards import load_tree
        from kpi.track import load_instrumentation

        for program_id in ids:
            payload = dashboard_payload(
                program_id, load_instrumentation(program_id).computes, load_tree(program_id)
            )
            print(json.dumps(payload, indent=2)[:400], "…", file=sys.stderr)
            print(f"{program_id}: {len(payload['widgets'])} widget(s) (use --push to land)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
