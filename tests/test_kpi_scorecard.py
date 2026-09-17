"""The scorecard's rules and its push loop (RC1-454).

Offline: the four entity rules are pure functions over a dict, and the two that
reach Datadog go through `httpx.MockTransport`.
"""

from __future__ import annotations

import json

import httpx
import pytest

from kpi import scorecard


def entity(**over) -> dict:
    """A passing entity, so each test can spoil exactly one thing."""
    doc = {
        "metadata": {
            "name": "svc",
            "owner": "reid",
            "links": [
                {"name": "Repository", "type": "repo", "url": "https://github.com/x/y"},
                {"name": "Board", "type": "dashboard", "url": "https://app.datadoghq.com/d/a"},
            ],
        },
        "spec": {"lifecycle": "production", "tier": "2"},
    }
    doc.update(over)
    return doc


NO_FACTS = scorecard.Facts()


def test_owner_rule_reads_the_declared_owner():
    ok, why = scorecard.has_an_owner(entity(), NO_FACTS)
    assert ok and "reid" in why

    meta = dict(entity()["metadata"], owner=None)
    ok, why = scorecard.has_an_owner(entity(metadata=meta), NO_FACTS)
    assert not ok and "no owner" in why


@pytest.mark.parametrize(
    "rule,link_type",
    [(scorecard.has_a_repo_link, "repo"), (scorecard.has_a_dashboard_link, "dashboard")],
)
def test_link_rules_match_on_type_not_on_name(rule, link_type):
    """A link counts because of its `type`, never because its name looks right."""
    doc = entity()
    assert rule(doc, NO_FACTS)[0]

    # Same URLs, wrong types: every rule must now fail.
    stripped = [dict(link, type="other") for link in doc["metadata"]["links"]]
    doc["metadata"] = dict(doc["metadata"], links=stripped)
    ok, why = rule(doc, NO_FACTS)
    assert not ok and link_type in why


def test_lifecycle_rule_names_what_is_missing():
    ok, _ = scorecard.declares_lifecycle_and_tier(entity(), NO_FACTS)
    assert ok

    ok, why = scorecard.declares_lifecycle_and_tier(entity(spec={"tier": "2"}), NO_FACTS)
    assert not ok and why == "missing lifecycle"

    ok, why = scorecard.declares_lifecycle_and_tier(entity(spec={}), NO_FACTS)
    assert not ok and why == "missing lifecycle and tier"


def test_slo_rule_reads_the_tag_and_ignores_the_name():
    """RC1-454: an SLO named after a service but untagged does NOT count.

    This is the whole reason the rule exists in this shape — the estate's first
    estimate came from matching names and was wrong in both directions.
    """
    facts = scorecard.Facts(slos_by_service={"svc": ["Availability — svc"]})
    ok, why = scorecard.has_an_slo(entity(), facts)
    assert ok and "Availability — svc" in why

    # An SLO tagged for a different service is not this service's SLO.
    ok, why = scorecard.has_an_slo(entity(), scorecard.Facts(slos_by_service={"other": ["x"]}))
    assert not ok and why == "no SLO tagged service:svc"


def _client(handler) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.datadoghq.com"
    )


def test_gather_groups_slos_by_their_service_tag():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/slo"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"name": "site", "tags": ["service:hihelloreid", "rc1:342"]},
                    {"name": "incidents", "tags": ["service:hihelloreid"]},
                    {"name": "program", "tags": ["generated:kpi-datadog"]},
                    {"name": "untagged", "tags": []},
                    {"name": "null tags", "tags": None},
                ]
            },
        )

    facts = scorecard.gather(_client(handler))
    assert facts.slos_by_service == {"hihelloreid": ["site", "incidents"]}


def test_sync_rules_creates_only_the_missing_ones():
    """A rule already in the scorecard is left alone; others are created once."""
    first = scorecard.RULES[0]
    created: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "existing-id",
                            "attributes": {
                                "name": first.name,
                                "scorecard_name": scorecard.SCORECARD_NAME,
                            },
                        },
                        # Same rule name under a different scorecard must not match.
                        {
                            "id": "other-card",
                            "attributes": {
                                "name": scorecard.RULES[1].name,
                                "scorecard_name": "Someone else's card",
                            },
                        },
                    ]
                },
            )
        body = json.loads(request.content)["data"]["attributes"]
        created.append(body["name"])
        assert body["scorecard_name"] == scorecard.SCORECARD_NAME
        return httpx.Response(201, json={"data": {"id": f"new-{len(created)}"}})

    ids = scorecard.sync_rules(_client(handler))

    assert ids[first.name] == "existing-id"
    assert created == [r.name for r in scorecard.RULES[1:]]
    assert set(ids) == {r.name for r in scorecard.RULES}


def test_sync_rules_refuses_a_failed_create():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(403, text="nope")

    with pytest.raises(SystemExit, match="403"):
        scorecard.sync_rules(_client(handler))


def test_render_prints_a_count_per_rule_and_spells_out_failures():
    rule = scorecard.RULES[0]
    scored = [
        ("alpha", rule, True, "fine"),
        ("beta", rule, False, "no owner declared"),
    ]
    out = scorecard.render(scored)
    assert f"{rule.name}: 1/2" in out
    assert "FAIL beta — no owner declared" in out
    assert "1/2 outcomes passing" in out
    # A passing service is counted, never listed: the failures are the report.
    assert "alpha" not in out


def test_rule_names_are_unique():
    """Rules are matched to Datadog by name, so a duplicate would silently
    overwrite the other's outcomes."""
    names = [r.name for r in scorecard.RULES]
    assert len(names) == len(set(names))
