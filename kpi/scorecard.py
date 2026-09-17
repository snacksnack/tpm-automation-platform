"""The Software Catalog scorecard (RC1-454).

Datadog **stores** scores; it does not compute them. The account ships no rules
of its own, and a custom rule never evaluates itself — you create the rule, then
POST one outcome per service per rule. So the product supplies the scoreboard
and this module supplies the scoring, which is the usual division here: rules
decide in Python, nothing is left to a model.

    python -m kpi.scorecard show    # evaluate and print, touching nothing
    python -m kpi.scorecard push    # create missing rules, then push outcomes

Every rule is a plain function over an entity and a `Facts` bundle. Four read
the entity files alone, so they are exact by construction; `has_an_slo` reads
the account. Adding a rule means adding a function and one `Rule(...)` row.

**Why one rule starts red, on purpose.** `has_an_slo` scores 0/8 today. Not one
SLO in the account carries a `service:` tag — six are `generated:kpi-datadog`
program SLOs that belong to no service, one is fleet-wide, and the two site
availability SLOs are tagged `site:hihelloreid`. A scorecard whose every rule
passes on the day it ships is telling you the rules are too weak, so this one
ships with a gap it can actually measure, and the number climbs as SLOs get
tagged. The earlier estimate of 3/8 came from matching SLO *names* and was
wrong; matching a tag is mechanical, and a rule nobody can argue with is the
whole point.

Matching by tag rather than by name is the convention for anything that grows
here: when `has_a_monitor` lands it will read `service:` tags too.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from kpi.catalog_sync import load
from kpi.datadog_sync import client

#: The scorecard these rules group under, as it appears in the Datadog UI.
SCORECARD_NAME = "RC1 estate readiness"

#: Tag that ties a Datadog object to a catalog entity. `service:<entity name>`.
SERVICE_TAG = "service:"


@dataclass(frozen=True)
class Facts:
    """Everything the rules read from the account, fetched once per run.

    One bundle rather than a call per rule per service: eight entities times
    five rules is forty evaluations, and the SLO list does not change between
    them.
    """

    #: entity name -> the SLO names tagged `service:<entity name>`
    slos_by_service: dict[str, list[str]] = field(default_factory=dict)


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
)


def gather(http: httpx.Client) -> Facts:
    """Read the account once for everything the rules need."""
    resp = http.get("/api/v1/slo", params={"limit": 1000})
    resp.raise_for_status()
    by_service: dict[str, list[str]] = {}
    for slo in resp.json().get("data", []):
        for tag in slo.get("tags", []) or []:
            if tag.startswith(SERVICE_TAG):
                by_service.setdefault(tag[len(SERVICE_TAG) :], []).append(slo.get("name", ""))
    return Facts(slos_by_service=by_service)


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
