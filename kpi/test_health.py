"""Fleet test-health counts → Datadog count series (RC1-467).

Test Optimization holds every default-branch test event, but nothing an SLO
can read: DORA-style event queries cannot back an SLO, a `ci-tests alert`
monitor is the same class the `ci-pipelines alert` monitor already proved
unsupported (RC1-456), and the generate-metrics config surface is not exposed
to this account. So the platform does what it did for the drift heartbeat and
the scanner gauges: read the API, decide in Python, post the numbers itself.

Once a day this reads the last 24 h of default-branch test events grouped by
service and status, and posts two count metrics per reporting service:

    delivery.tests.attempted{test_service}   pass + fail
    delivery.tests.passed{test_service}      pass only

Skips are in neither number on purpose: a skip is a decision, not an outcome,
and counting it either way would move the pass ratio without a test having
run. The "Fleet test health" SLO is metric-based over these two
(`sum:passed / sum:attempted`), so each day's post is one more term in the
window's ratio. A service with no events today posts nothing — a quiet day
adds no denominator, it does not read as a failure.

Cost: two series per active service per day (~22 series present one hour in
twenty-four) — a fraction of one custom metric a month. Do not make it
hourly; the events only change when CI runs.

Run: `python -m kpi.test_health [--dry-run]`
Env: `DD_API_KEY` + `DD_APP_KEY` (the aggregate endpoint needs both),
`DD_SITE` optional.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

from kpi.datadog import api_url, ship

#: v2 series `type`: 1 is count — points sum over a query window, which is
#: exactly what a good/total SLO does with them. The enum is an int, not a
#: string; a wrong value is swallowed as a 400 that looks like no traffic.
COUNT = 1

#: One day, both the query window and the count interval.
DAY_S = 86_400

ATTEMPTED_METRIC = "delivery.tests.attempted"
PASSED_METRIC = "delivery.tests.passed"

#: Tag key for the reporting test service (the GitHub repo name, RC1-452).
#: Deliberately not `service:` — that tag ties Datadog objects to catalog
#: entities (kpi/scorecard.py), and repos are not entities.
SERVICE_TAG_KEY = "test_service"

#: Only default-branch runs count toward fleet health. PR branches carry
#: deliberate probes and work in progress; main is the claim being measured.
BRANCH_QUERY = "@git.branch:main"


def fetch_status_counts(http: httpx.Client) -> dict[str, dict[str, int]]:
    """{test service: {status: count}} for the last 24 h of default-branch
    test events. Statuses are Datadog's own (`pass` / `fail` / `skip`)."""
    resp = http.post(
        "/api/v2/ci/tests/analytics/aggregate",
        json={
            "compute": [{"aggregation": "count"}],
            "filter": {
                "query": BRANCH_QUERY,
                "from": f"now-{DAY_S}s",
                "to": "now",
            },
            "group_by": [
                {"facet": "@test.service", "limit": 50},
                {"facet": "@test.status", "limit": 10},
            ],
        },
    )
    resp.raise_for_status()
    out: dict[str, dict[str, int]] = {}
    for bucket in resp.json().get("data", {}).get("buckets", []):
        by = bucket.get("by", {})
        service = by.get("@test.service")
        status = by.get("@test.status")
        if not service or not status:
            continue
        count = int(bucket.get("computes", {}).get("c0", 0))
        out.setdefault(service, {})[status] = count
    return out


def series_for(counts: dict[str, dict[str, int]]) -> list[dict]:
    """The v2 series payload: attempted (pass+fail) and passed per service.

    Pure apart from the clock. Skips are excluded from both numbers — see the
    module docstring. A service whose day held only skips posts nothing, the
    same as a service that did not run.
    """
    at = int(time.time())
    out: list[dict] = []
    for service in sorted(counts):
        passed = counts[service].get("pass", 0)
        attempted = passed + counts[service].get("fail", 0)
        if not attempted:
            continue
        tags = [f"{SERVICE_TAG_KEY}:{service}"]
        for metric, value in ((ATTEMPTED_METRIC, attempted), (PASSED_METRIC, passed)):
            out.append(
                {
                    "metric": metric,
                    "type": COUNT,
                    "interval": DAY_S,
                    "points": [{"timestamp": at, "value": float(value)}],
                    "tags": tags,
                }
            )
    return out


def summary_lines(counts: dict[str, dict[str, int]]) -> list[str]:
    lines = []
    for service in sorted(counts):
        c = counts[service]
        lines.append(
            f"{service:34s} pass {c.get('pass', 0):5d}  fail {c.get('fail', 0):3d}"
            f"  skip {c.get('skip', 0):3d}"
        )
    return lines or ["no default-branch test events in the last 24 h"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m kpi.test_health",
        description="Read the last day of default-branch test events and post "
        "fleet test-health counts to Datadog (RC1-467).",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the counts and the series payload; do not post to Datadog",
    )
    args = ap.parse_args(argv)

    api_key = os.environ.get("DD_API_KEY")
    app_key = os.environ.get("DD_APP_KEY")
    if not api_key or not app_key:
        print(
            "DD_API_KEY and DD_APP_KEY are both required — the test-events "
            "aggregate endpoint reads with the pair.",
            file=sys.stderr,
        )
        return 2

    with httpx.Client(
        base_url=api_url(""),
        headers={"DD-API-KEY": api_key, "DD-APPLICATION-KEY": app_key},
        timeout=30,
    ) as http:
        counts = fetch_status_counts(http)
    series = series_for(counts)
    print("\n".join(summary_lines(counts)))

    if args.dry_run:
        print(json.dumps({"series": series}, indent=1))
        print(f"dry run — {len(series)} series not sent")
        return 0

    if not series:
        print("nothing to post")
        return 0
    ship(series, api_key=api_key)
    print(f"shipped {len(series)} series to Datadog")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
