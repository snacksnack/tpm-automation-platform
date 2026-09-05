"""GitHub scanner alert counts → Datadog gauges (RC1-359).

Why this exists at all: Datadog's GitHub integration advertises code-scan
and secret-scan alert *telemetry*, and its two metric names register in the
account the moment the collectors are switched on — but the collector is
organization-scoped. GitHub only lists alerts account-wide at
`/orgs/{org}/...`, and those endpoints return 404 for a personal account.
Three days after enabling, zero data points; see the ticket for the trail.
The repo-level endpoints work fine for a user account, so this module reads
them and posts the counts itself.

Cost, because that was the question: Datadog bills custom metrics on the
average number of unique series present per hour over the month. This runs
once a day, so its ~25 series occupy one hour in twenty-four and add about
one custom metric to the monthly average — cents. Do not make it hourly to
"get fresher data": alert counts change a few times a week and the bill
would go up 24×.

Scanner output stays out of PR threads (the RC1-338 rule): this reads the
Security tab, it never writes anywhere on GitHub.

Run: `python -m kpi.security_posture [--dry-run]`
Env: `SECURITY_ALERTS_TOKEN` — a fine-grained PAT with *read* on Code
scanning alerts and Secret scanning alerts for the five repos (the default
Actions token is scoped to the running repo and cannot read the other four);
`DD_API_KEY` to ship, `DD_SITE` optional.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

from kpi.datadog import GAUGE, ship

OWNER = "snacksnack"

#: The five repos the scanners were enabled on (RC1-359 step 2).
REPOS = (
    "tpm-automation-platform",
    "pr_agent",
    "launch-planner-agent",
    "agent-evals",
    "reid_basic",
)

CODE_METRIC = "delivery.security.code_scan_alerts_open"
SECRET_METRIC = "delivery.security.secret_scan_alerts_open"

#: Always emitted, zero-filled, so "0 critical" is a point and a monitor on
#: `critical > 0` has data to read. Anything CodeQL leaves without a security
#: severity (some `actions/*` rules) lands in `none`, emitted only when > 0.
SEVERITIES = ("critical", "high", "medium", "low")

TOKEN_ENV = "SECURITY_ALERTS_TOKEN"
GITHUB_API = "https://api.github.com"


def github_client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=GITHUB_API,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )


def fetch_open_alerts(http: httpx.Client, repo: str, kind: str) -> list[dict]:
    """Every open alert of `kind` ("code-scanning" | "secret-scanning") for
    one repo, following pagination.

    A 404 is "nothing to count", not an error: code-scanning returns 404 with
    "no analysis found" on a repo CodeQL has never finished on, and
    secret-scanning returns 404 when the feature is off. Both are zero alerts
    from the dashboard's point of view; the enablement state itself is
    checked elsewhere (`security_and_analysis`).
    """
    alerts: list[dict] = []
    url: str | None = f"/repos/{OWNER}/{repo}/{kind}/alerts?state=open&per_page=100"
    while url:
        resp = http.get(url)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        alerts.extend(resp.json())
        url = resp.links.get("next", {}).get("url")
    return alerts


def count_by_severity(alerts: list[dict]) -> dict[str, int]:
    """Open code-scan alerts bucketed by CodeQL's security severity.

    Keys are always drawn from our own vocabulary — the four in `SEVERITIES`
    plus `none` — never copied from the response. That keeps the metric's tag
    set bounded if GitHub ever adds a level, and keeps response strings out
    of the log and the payload: CodeQL's own clear-text-logging query taints
    everything read through a client that carries the token, and it is right
    that the only things we print are our labels and our counts.
    """
    counts = dict.fromkeys(SEVERITIES, 0)
    for alert in alerts:
        level = (alert.get("rule") or {}).get("security_severity_level")
        key = level if level in SEVERITIES else "none"
        counts[key] = counts.get(key, 0) + 1
    return counts


def collect(http: httpx.Client, repos: tuple[str, ...] = REPOS) -> dict[str, dict]:
    """{repo: {"code": {severity: n}, "secret": n}} for every repo."""
    out: dict[str, dict] = {}
    for repo in repos:
        code = fetch_open_alerts(http, repo, "code-scanning")
        secret = fetch_open_alerts(http, repo, "secret-scanning")
        out[repo] = {"code": count_by_severity(code), "secret": len(secret)}
    return out


def _series(metric: str, value: float, *, at: int, tags: list[str]) -> dict:
    return {
        "metric": metric,
        "type": GAUGE,
        "points": [{"timestamp": at, "value": value}],
        "tags": tags,
    }


def series_for(posture: dict[str, dict]) -> list[dict]:
    """The v2 series payload, stamped with the current time. Pure apart from
    the clock — the tests read this, not a network.

    The four canonical severities are always emitted, zeros included; the
    `none` bucket (alerts CodeQL left without a security severity) only when
    it holds something, so a repo with none of those has no series for it.
    """
    at = int(time.time())
    out: list[dict] = []
    for repo, counts in posture.items():
        repo_tag = f"repo:{repo}"
        code = counts["code"]
        for severity in SEVERITIES:
            n = code.get(severity, 0)
            out.append(
                _series(CODE_METRIC, float(n), at=at, tags=[repo_tag, f"severity:{severity}"])
            )
        if code.get("none", 0):
            out.append(
                _series(CODE_METRIC, float(code["none"]), at=at, tags=[repo_tag, "severity:none"])
            )
        out.append(_series(SECRET_METRIC, float(counts["secret"]), at=at, tags=[repo_tag]))
    return out


def summary_lines(posture: dict[str, dict]) -> list[str]:
    lines = []
    for repo, counts in posture.items():
        code = counts["code"]
        nonzero = (
            ", ".join(f"{sev} {code[sev]}" for sev in (*SEVERITIES, "none") if code.get(sev))
            or "none"
        )
        lines.append(
            f"{repo:26s} code-scan open: {nonzero:32s} secret-scan open: {counts['secret']}"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m kpi.security_posture",
        description="Read open GitHub scanner alerts for the five repos and post them "
        "to Datadog as daily gauges (RC1-359).",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the counts and the series payload; do not post to Datadog",
    )
    args = ap.parse_args(argv)

    token = os.environ.get(TOKEN_ENV)
    if not token:
        print(
            f"{TOKEN_ENV} is not set — a fine-grained PAT with read on code-scanning and "
            "secret-scanning alerts for the five repos.",
            file=sys.stderr,
        )
        return 2

    with github_client(token) as http:
        posture = collect(http)
    series = series_for(posture)
    print("\n".join(summary_lines(posture)))

    if args.dry_run:
        print(json.dumps({"series": series}, indent=1))
        print(f"dry run — {len(series)} series not sent")
        return 0

    api_key = os.environ.get("DD_API_KEY")
    if not api_key:
        print("DD_API_KEY is not set — nothing shipped", file=sys.stderr)
        return 2
    ship(series, api_key=api_key)
    print(f"shipped {len(series)} series to Datadog")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
