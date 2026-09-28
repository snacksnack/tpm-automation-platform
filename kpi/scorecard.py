"""The Software Catalog scorecard (RC1-454).

Datadog **stores** scores; it does not compute them. The account ships no rules
of its own, and a custom rule never evaluates itself — you create the rule, then
POST one outcome per service per rule. So the product supplies the scoreboard
and this module supplies the scoring, which is the usual division here: rules
decide in Python, nothing is left to a model.

    python -m kpi.scorecard show    # evaluate and print, touching nothing
    python -m kpi.scorecard push    # create missing rules, then push outcomes

Every rule is a plain function over an entity and a `Facts` bundle. Four read
the entity files alone, so they are exact by construction; the rest read the
account (`has_an_slo`, `has_a_monitor`, and the RC1-469 pair —
`reports_deploys_to_dora` over DORA deployment events and
`security_posture_clean` over the RC1-359 scanner gauges). Adding a rule means
adding a function and one `Rule(...)` row.

**Why two rules stay red, on purpose.** `has_an_slo` shipped at 0/8 and now
scores 6/8; `has_a_monitor` (RC1-457) opens at the same 6/8. Both stop at the
same two services, and both reds are correct rather than unfinished:
`launch-planner-agent` is waiting on RC1-455 to say whether anyone uses it, and
`stale-ticket-bot` is dormant and emits nothing to watch. A scorecard whose
every rule passes on the day it ships is telling you the rules are too weak, so
these ship with the gaps they can actually measure and the numbers climb as the
tickets close. The first estimate of `has_an_slo` came from matching SLO
*names* and was wrong in both directions; matching a tag is mechanical, and a
rule nobody can argue with is the whole point.

Matching by tag rather than by name is the convention for everything that grows
here.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from kpi.catalog_sync import load
from kpi.datadog_sync import client

#: The scorecard these rules group under, as it appears in the Datadog UI.
SCORECARD_NAME = "RC1 estate readiness"

#: Tag that ties a Datadog object to a catalog entity. `service:<entity name>`.
SERVICE_TAG = "service:"

#: Tag on the entity itself naming its GitHub repository. `repo:<repo name>`.
REPO_TAG = "repo:"

#: How far back a deployment still counts as "this service reports to DORA".
#: Wide on purpose: agent-evals deploys only on release tags, and a service
#: that deploys rarely is not the same finding as one whose deploys are
#: invisible (RC1-469).
DORA_WINDOW_DAYS = 90

#: The RC1-359 security-posture gauges, one series per repo per day. The
#: "leaks" key follows kpi/security_posture.py's naming rule: identifiers say
#: what they hold (an integer count), so CodeQL's sensitive-name heuristics
#: stay quiet.
ALERT_METRICS = {
    "code": "delivery.security.code_scan_alerts_open",
    "leaks": "delivery.security.secret_scan_alerts_open",
    "errors": "delivery.security.collector_errors",
}


@dataclass(frozen=True)
class Facts:
    """Everything the rules read from the account, fetched once per run.

    One bundle rather than a call per rule per service: eight entities times
    eight rules is sixty-four evaluations, and none of these lists change
    between them.
    """

    #: entity name -> the SLO names tagged `service:<entity name>`
    slos_by_service: dict[str, list[str]] = field(default_factory=dict)

    #: entity name -> the monitor names tagged `service:<entity name>`
    monitors_by_service: dict[str, list[str]] = field(default_factory=dict)

    #: DORA `service` facet value -> deployment count in the last
    #: DORA_WINDOW_DAYS. A service absent here reported no deployments.
    dora_deploys_by_service: dict[str, int] = field(default_factory=dict)

    #: repo name -> latest daily counts from the RC1-359 gauges
    #: ({"code": n, "leaks": n, "errors": n}). A repo absent here has no
    #: scanner telemetry at all, which is a finding, not a zero.
    open_alerts_by_repo: dict[str, dict[str, int]] = field(default_factory=dict)


def _links(entity: dict) -> list[dict]:
    return entity.get("metadata", {}).get("links", []) or []


def has_an_owner(entity: dict, _: Facts) -> tuple[bool, str]:
    owner = entity.get("metadata", {}).get("owner")
    return bool(owner), f"owner is {owner!r}" if owner else "no owner declared"


def has_a_repo_link(entity: dict, _: Facts) -> tuple[bool, str]:
    repos = [link for link in _links(entity) if link.get("type") == "repo"]
    if not repos:
        return False, "no link of type 'repo'"
    return True, repos[0].get("url", "")


def has_a_dashboard_link(entity: dict, _: Facts) -> tuple[bool, str]:
    boards = [link for link in _links(entity) if link.get("type") == "dashboard"]
    if not boards:
        return False, "no link of type 'dashboard' — add one to the entity file"
    return True, boards[0].get("name", "")


def declares_lifecycle_and_tier(entity: dict, _: Facts) -> tuple[bool, str]:
    spec = entity.get("spec", {})
    lifecycle, tier = spec.get("lifecycle"), spec.get("tier")
    if lifecycle and tier:
        return True, f"{lifecycle}, tier {tier}"
    missing = [n for n, v in (("lifecycle", lifecycle), ("tier", tier)) if not v]
    return False, f"missing {' and '.join(missing)}"


def has_an_slo(entity: dict, facts: Facts) -> tuple[bool, str]:
    """An SLO counts when it is tagged `service:<entity name>`.

    Deliberately not name matching. The service this SLO is *about* is a fact
    the SLO should state, not something a scorecard should infer from a string
    it happens to contain.
    """
    name = entity.get("metadata", {}).get("name", "")
    found = facts.slos_by_service.get(name, [])
    if found:
        return True, f"{len(found)} SLO(s): {', '.join(found)}"
    return False, f"no SLO tagged {SERVICE_TAG}{name}"


def has_a_monitor(entity: dict, facts: Facts) -> tuple[bool, str]:
    """A monitor counts when it is tagged `service:<entity name>` (RC1-457).

    Same shape as `has_an_slo`, and for the same reason: the catalog joins a
    monitor to a service by that tag, so a monitor that watches a service but
    does not say which one is invisible to everything downstream.

    Two things to know before chasing a red here. A Synthetics monitor counts
    like any other — three of them are why `hihelloreid` passes — but its tags
    come from the *test*, so it is fixed in `datadog/synthetics/*.json` and
    Datadog regenerates the monitor; editing the monitor JSON is undone on the
    next sync. And the fleet-level monitors (`service:agent-fleet`,
    `service:delivery-pipeline`) will never satisfy this rule for anyone: they
    span every ml_app and every repo on purpose, so retagging one per service
    would break the query it exists to run. They stay orphans of the catalog,
    and that is the correct answer rather than a gap.
    """
    name = entity.get("metadata", {}).get("name", "")
    found = facts.monitors_by_service.get(name, [])
    if found:
        return True, f"{len(found)} monitor(s): {', '.join(found)}"
    return False, f"no monitor tagged {SERVICE_TAG}{name}"


def reports_deploys_to_dora(entity: dict, facts: Facts) -> tuple[bool, str]:
    """A DORA deployment event names this service (the facet matches catalog
    entity names exactly — probed before this rule was written, not assumed)."""
    name = entity.get("metadata", {}).get("name", "")
    count = facts.dora_deploys_by_service.get(name, 0)
    if count:
        return True, f"{count} deployment(s) in the last {DORA_WINDOW_DAYS} days"
    return False, (
        f"no DORA deployment event in {DORA_WINDOW_DAYS} days — the service "
        "deploys, but its deploy path emits nothing (RC1-459 wired the "
        "workflow-driven paths; a manual path needs its own event)"
    )


def _repo_tag(entity: dict) -> str:
    tags = entity.get("metadata", {}).get("tags") or []
    return next((t[len(REPO_TAG) :] for t in tags if t.startswith(REPO_TAG)), "")


def security_posture_clean(entity: dict, facts: Facts) -> tuple[bool, str]:
    """Scanner telemetry exists for the entity's repo and shows zero open
    alerts. Missing or unreadable telemetry fails: a gap is never a zero."""
    repo = _repo_tag(entity)
    if not repo:
        return False, f"entity declares no {REPO_TAG} tag, so it maps to no repository"
    counts = facts.open_alerts_by_repo.get(repo)
    if counts is None:
        return False, (
            f"no scanner telemetry for repo:{repo} — the RC1-359 collector "
            "enrolls five repos; extending kpi/security_posture.REPOS is the fix"
        )
    if counts.get("errors"):
        return False, (
            f"the collector could not read repo:{repo} on its last run — "
            "a gap, never a zero"
        )
    open_code, open_leaks = counts.get("code", 0), counts.get("leaks", 0)
    if open_code or open_leaks:
        found = ", ".join(
            part
            for part in (
                f"{open_code} code-scanning" if open_code else "",
                f"{open_leaks} secret-scanning" if open_leaks else "",
            )
            if part
        )
        return False, (
            f"{found} alert(s) open on repo:{repo} — fix or disposition them; "
            "the scanner itself stays on"
        )
    return True, f"scanners read, 0 open alerts on repo:{repo}"


@dataclass(frozen=True)
class Rule:
    name: str
    description: str
    evaluate: Callable[[dict, Facts], tuple[bool, str]]


RULES: tuple[Rule, ...] = (
    Rule(
        "Has an owner",
        "Every service names an owner, so a question about it has somewhere to go.",
        has_an_owner,
    ),
    Rule(
        "Has a repository link",
        "The catalog entry leads to the code, not just to a name.",
        has_a_repo_link,
    ),
    Rule(
        "Declares lifecycle and tier",
        "A service says whether it is production and how much it matters, rather "
        "than leaving both to be assumed.",
        declares_lifecycle_and_tier,
    ),
    Rule(
        "Has a dashboard link",
        "Somewhere to look when the service misbehaves, reachable from the catalog.",
        has_a_dashboard_link,
    ),
    Rule(
        "Has an SLO",
        f"An SLO tagged {SERVICE_TAG}<service> exists. Tagged, not named: what an "
        "SLO covers should be stated by the SLO, not inferred from its title.",
        has_an_slo,
    ),
    Rule(
        "Has a monitor",
        f"A monitor tagged {SERVICE_TAG}<service> exists, so a failure is noticed "
        "while it is happening rather than measured afterwards by the SLO.",
        has_a_monitor,
    ),
    Rule(
        "Deploys report to DORA",
        f"A deployment event reached DORA within {DORA_WINDOW_DAYS} days, so the "
        "service's change rate is measured rather than remembered (RC1-469).",
        reports_deploys_to_dora,
    ),
    Rule(
        "Security posture clean",
        "The RC1-359 collector reads the repo's scanners and the latest daily "
        "count of open alerts is zero. Missing or unreadable telemetry fails "
        "too: a gap is never a zero (RC1-469).",
        security_posture_clean,
    ),
)


def _by_service_tag(objects: list[dict]) -> dict[str, list[str]]:
    """Group Datadog objects under the service each one names in its tags.

    An object with no `service:` tag belongs to no service here, which is the
    point: the six `generated:kpi-datadog` program SLOs and the fleet-wide
    monitors are not any one service's, and inventing an owner for them would
    make the scorecard say something untrue.
    """
    grouped: dict[str, list[str]] = {}
    for obj in objects:
        for tag in obj.get("tags") or []:
            if tag.startswith(SERVICE_TAG):
                grouped.setdefault(tag[len(SERVICE_TAG) :], []).append(obj.get("name", ""))
    return grouped


def _dora_deploy_counts(http: httpx.Client) -> dict[str, int]:
    """Deployment counts per DORA service over the rule's window.

    The same query the DORA dashboard's per-service widget runs (data_source
    `dora`, index `deployment`, grouped by the `service` facet), replayed
    through /api/v2/query/timeseries with one bucket spanning the window.
    """
    now_ms = int(time.time() * 1000)
    window_ms = DORA_WINDOW_DAYS * 86_400_000
    resp = http.post(
        "/api/v2/query/timeseries",
        json={
            "data": {
                "type": "timeseries_request",
                "attributes": {
                    "formulas": [{"formula": "deploys"}],
                    "queries": [
                        {
                            "data_source": "dora",
                            "name": "deploys",
                            "compute": {"aggregation": "count"},
                            "indexes": ["deployment"],
                            "group_by": [
                                {
                                    "facet": "service",
                                    "limit": 50,
                                    "sort": {"aggregation": "count", "order": "desc"},
                                }
                            ],
                        }
                    ],
                    "from": now_ms - window_ms,
                    "to": now_ms,
                    "interval": window_ms,
                },
            }
        },
    )
    resp.raise_for_status()
    attrs = resp.json().get("data", {}).get("attributes", {})
    values = attrs.get("values", [])
    counts: dict[str, int] = {}
    for i, series in enumerate(attrs.get("series", [])):
        tags = series.get("group_tags") or []
        service = next((t[len(SERVICE_TAG) :] for t in tags if t.startswith(SERVICE_TAG)), "")
        total = int(sum(v for v in (values[i] if i < len(values) else []) if v))
        if service and total:
            counts[service] = total
    return counts


def _open_alert_counts(http: httpx.Client) -> dict[str, dict[str, int]]:
    """Latest daily point of each RC1-359 gauge, per repo.

    A 2-day window because the collector runs once a day — a shorter window
    reads "no data" in the hours before the next run, which is cadence, not
    an outage. The latest point is the answer; summing points would count
    yesterday's alerts twice.
    """
    now = int(time.time())
    out: dict[str, dict[str, int]] = {}
    for key, metric in ALERT_METRICS.items():
        resp = http.get(
            "/api/v1/query",
            params={
                "from": now - 2 * 86_400,
                "to": now,
                "query": f"sum:{metric}{{*}} by {{repo}}",
            },
        )
        resp.raise_for_status()
        for series in resp.json().get("series", []):
            scope = series.get("scope", "")
            if not scope.startswith(REPO_TAG):
                continue
            points = [p[1] for p in series.get("pointlist", []) if p[1] is not None]
            if points:
                out.setdefault(scope[len(REPO_TAG) :], {})[key] = int(points[-1])
    return out


def gather(http: httpx.Client) -> Facts:
    """Read the account once for everything the rules need."""
    slos = http.get("/api/v1/slo", params={"limit": 1000})
    slos.raise_for_status()
    monitors = http.get("/api/v1/monitor", params={"page_size": 1000})
    monitors.raise_for_status()
    return Facts(
        slos_by_service=_by_service_tag(slos.json().get("data", [])),
        monitors_by_service=_by_service_tag(monitors.json()),
        dora_deploys_by_service=_dora_deploy_counts(http),
        open_alerts_by_repo=_open_alert_counts(http),
    )


def evaluate(facts: Facts) -> list[tuple[str, Rule, bool, str]]:
    """Score every entity against every rule. Returns (service, rule, ok, why)."""
    out = []
    for name, entity in sorted(load().items()):
        for rule in RULES:
            ok, why = rule.evaluate(entity, facts)
            out.append((name, rule, ok, why))
    return out


def sync_rules(http: httpx.Client) -> dict[str, str]:
    """Ensure every rule exists in the scorecard. Returns rule name -> id.

    Rules are matched on name within `SCORECARD_NAME`; a renamed rule creates a
    new one and orphans the old, the same trap catalog entities have, so rename
    deliberately and delete the old rule by hand.
    """
    resp = http.get("/api/v2/scorecard/rules", params={"page[size]": 100})
    resp.raise_for_status()
    existing = {
        item["attributes"]["name"]: item["id"]
        for item in resp.json().get("data", [])
        if item.get("attributes", {}).get("scorecard_name") == SCORECARD_NAME
    }
    for rule in RULES:
        if rule.name in existing:
            continue
        created = http.post(
            "/api/v2/scorecard/rules",
            json={
                "data": {
                    "type": "rule",
                    "attributes": {
                        "name": rule.name,
                        "scorecard_name": SCORECARD_NAME,
                        "description": rule.description,
                        "enabled": True,
                    },
                }
            },
        )
        if created.status_code >= 300:
            raise SystemExit(f"{rule.name}: HTTP {created.status_code} {created.text[:300]}")
        existing[rule.name] = created.json()["data"]["id"]
    return existing


def push() -> tuple[int, int]:
    """Create any missing rules, evaluate, and push every outcome.

    Returns (passing, total). Outcomes go up in one batch: a partial scorecard
    is worse than a stale one, because the rows that did land look current.
    """
    with client() as http:
        rule_ids = sync_rules(http)
        scored = evaluate(gather(http))
        results = [
            {
                "rule_id": rule_ids[rule.name],
                "service_name": service,
                "state": "pass" if ok else "fail",
                "remarks": why[:500],
            }
            for service, rule, ok, why in scored
        ]
        resp = http.post(
            "/api/v2/scorecard/outcomes/batch",
            json={"data": {"type": "batched-outcome", "attributes": {"results": results}}},
        )
        if resp.status_code >= 300:
            raise SystemExit(f"outcomes: HTTP {resp.status_code} {resp.text[:300]}")
    return sum(1 for _, _, ok, _ in scored if ok), len(scored)


def render(scored: list[tuple[str, Rule, bool, str]]) -> str:
    """The scorecard as a table, per rule, failures spelled out."""
    lines = []
    for rule in RULES:
        rows = [(s, ok, why) for s, r, ok, why in scored if r.name == rule.name]
        passing = sum(1 for _, ok, _ in rows if ok)
        lines.append(f"{rule.name}: {passing}/{len(rows)}")
        for service, ok, why in rows:
            if not ok:
                lines.append(f"    FAIL {service} — {why}")
    total = sum(1 for _, _, ok, _ in scored if ok)
    lines.append(f"\n{total}/{len(scored)} outcomes passing")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m kpi.scorecard",
        description="Score the catalog entities and publish to Datadog Scorecards.",
    )
    ap.add_argument("cmd", choices=["show", "push"])
    cmd = ap.parse_args(argv).cmd

    if cmd == "show":
        with client() as http:
            print(render(evaluate(gather(http))))
        return 0

    passing, total = push()
    print(f"pushed — {passing}/{total} outcomes passing across {len(RULES)} rules")
    return 0


if __name__ == "__main__":
    sys.exit(main())
