"""The scorecard's rules and its push loop (RC1-454, RC1-457).

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
            "tags": ["repo:svc-repo"],
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


def test_monitor_rule_reads_the_tag_and_ignores_the_name():
    """RC1-457: the same contract as the SLO rule, one object type over.

    The estate's monitors were the case that proved it — five Synthetics
    monitors watched `hihelloreid` for weeks under the tag
    `service:hihelloreid.com`, and the catalog could not see one of them.
    """
    facts = scorecard.Facts(monitors_by_service={"svc": ["svc — errors"]})
    ok, why = scorecard.has_a_monitor(entity(), facts)
    assert ok and "svc — errors" in why

    # Tagged for the fleet, not for this service: fleet-wide monitors are
    # nobody's on purpose, and must not be borrowed to make a red go green.
    fleet = scorecard.Facts(monitors_by_service={"agent-fleet": ["Fleet LLM spend"]})
    ok, why = scorecard.has_a_monitor(entity(), fleet)
    assert not ok and why == "no monitor tagged service:svc"


def test_slo_and_monitor_rules_do_not_read_each_other():
    """Facts carries two maps; a service with one object must not pass both."""
    only_slo = scorecard.Facts(slos_by_service={"svc": ["Availability — svc"]})
    assert scorecard.has_an_slo(entity(), only_slo)[0]
    assert not scorecard.has_a_monitor(entity(), only_slo)[0]


def _client(handler) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.datadoghq.com"
    )


def test_dora_rule_counts_deployments_for_the_entity_name():
    facts = scorecard.Facts(dora_deploys_by_service={"svc": 12})
    ok, why = scorecard.reports_deploys_to_dora(entity(), facts)
    assert ok
    assert "12 deployment(s)" in why


def test_dora_rule_fails_with_a_fix_shaped_remark_when_no_events_exist():
    ok, why = scorecard.reports_deploys_to_dora(entity(), NO_FACTS)
    assert not ok
    assert "emits nothing" in why


def test_security_rule_passes_only_on_read_telemetry_showing_zero():
    facts = scorecard.Facts(
        open_alerts_by_repo={"svc-repo": {"code": 0, "leaks": 0, "errors": 0}}
    )
    ok, why = scorecard.security_posture_clean(entity(), facts)
    assert ok
    assert "0 open alerts" in why


def test_test_results_rule_passes_on_recent_events():
    facts = scorecard.Facts(test_events_by_repo={"svc-repo": 123})
    ok, why = scorecard.reports_test_results(entity(), facts)
    assert ok and "123" in why


def test_test_results_rule_fail_remark_names_both_possible_causes():
    """Quiet CI and broken reporting look identical in the events store; the
    remark must say so instead of asserting one of them."""
    ok, why = scorecard.reports_test_results(entity(), NO_FACTS)
    assert not ok
    assert "no test events" in why and "Actions history" in why


def test_coverage_rule_puts_the_measured_number_in_the_remark():
    facts = scorecard.Facts(
        test_events_by_repo={"svc-repo": 10}, coverage_by_repo={"svc-repo": 83.6}
    )
    ok, why = scorecard.reports_code_coverage(entity(), facts)
    assert ok and "83.6%" in why


def test_coverage_rule_distinguishes_a_missing_flag_from_missing_tests():
    reporting_without_coverage = scorecard.Facts(test_events_by_repo={"svc-repo": 10})
    ok, why = scorecard.reports_code_coverage(entity(), reporting_without_coverage)
    assert not ok and "RC1-468" in why

    ok, why = scorecard.reports_code_coverage(entity(), NO_FACTS)
    assert not ok and "fix test reporting first" in why


def test_test_rules_fail_an_entity_with_no_repo_tag():
    meta = dict(entity()["metadata"], tags=[])
    for rule in (scorecard.reports_test_results, scorecard.reports_code_coverage):
        ok, why = rule(entity(metadata=meta), NO_FACTS)
        assert not ok and "repo:" in why


def test_security_rule_spells_out_open_alerts():
    facts = scorecard.Facts(open_alerts_by_repo={"svc-repo": {"code": 2, "leaks": 1}})
    ok, why = scorecard.security_posture_clean(entity(), facts)
    assert not ok
    assert "2 code-scanning" in why and "1 secret-scanning" in why


def test_security_rule_treats_missing_telemetry_as_a_finding_not_a_zero():
    """The repo the collector never enrolled must fail, and the remark must
    name the fix (extend REPOS), because 'no data' and 'no alerts' are
    different claims."""
    ok, why = scorecard.security_posture_clean(entity(), NO_FACTS)
    assert not ok
    assert "no scanner telemetry" in why and "REPOS" in why


def test_security_rule_treats_a_collector_error_as_unreadable_not_clean():
    facts = scorecard.Facts(
        open_alerts_by_repo={"svc-repo": {"code": 0, "leaks": 0, "errors": 1}}
    )
    ok, why = scorecard.security_posture_clean(entity(), facts)
    assert not ok
    assert "could not read" in why


def test_security_rule_requires_the_repo_tag():
    doc = entity()
    doc["metadata"]["tags"] = []
    ok, why = scorecard.security_posture_clean(doc, NO_FACTS)
    assert not ok
    assert "repo:" in why


def test_gather_groups_slos_and_monitors_by_their_service_tag():
    """Both lists come back shaped the same way, from differently shaped JSON.

    `/api/v1/slo` wraps its list in `data`; `/api/v1/monitor` returns a bare
    list. Getting that wrong is silent — every service simply scores red.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/slo":
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
        if request.url.path == "/api/v1/monitor":
            return httpx.Response(
                200,
                json=[
                    {"name": "SSL cert", "tags": ["service:hihelloreid", "rc1:341"]},
                    {"name": "Fleet LLM spend", "tags": ["service:agent-fleet"]},
                    {"name": "host pack", "tags": ["monitor_pack:host"]},
                    {"name": "null tags", "tags": None},
                ],
            )
        if request.url.path == "/api/v2/query/timeseries":
            # One bucket per series; a null bucket must not crash the sum.
            return httpx.Response(
                200,
                json={
                    "data": {
                        "attributes": {
                            "series": [
                                {"group_tags": ["service:hihelloreid"]},
                                {"group_tags": ["service:quiet"]},
                                {"group_tags": ["env:prod"]},
                            ],
                            "values": [[25.0], [None], [3.0]],
                        }
                    }
                },
            )
        if request.url.path == "/api/v2/ci/tests/analytics/aggregate":
            # Answers both RC1-467 aggregates: the count query and the
            # coverage average tell themselves apart by the compute block.
            body = json.loads(request.content)
            metric = body["compute"][0].get("metric")
            value = 71.7 if metric else 549
            return httpx.Response(
                200,
                json={
                    "data": {
                        "buckets": [
                            {"by": {"@test.service": "reid_basic"}, "computes": {"c0": value}}
                        ]
                    }
                },
            )
        assert request.url.path == "/api/v1/query"
        # The three RC1-359 gauges answer the same shape; two points per
        # series because the window spans two daily runs — the LATEST point
        # is the answer, not the sum.
        return httpx.Response(
            200,
            json={
                "series": [
                    {"scope": "repo:pr_agent", "pointlist": [[1.0, 2.0], [2.0, 1.0]]},
                    {"scope": "repo:reid_basic", "pointlist": [[1.0, 0.0], [2.0, 0.0]]},
                    {"scope": "env:prod", "pointlist": [[1.0, 9.0]]},
                ]
            },
        )

    facts = scorecard.gather(_client(handler))
    assert facts.slos_by_service == {"hihelloreid": ["site", "incidents"]}
    assert facts.monitors_by_service == {
        "hihelloreid": ["SSL cert"],
        "agent-fleet": ["Fleet LLM spend"],
    }
    # The null-bucket series and the service-less series both drop out.
    assert facts.dora_deploys_by_service == {"hihelloreid": 25}
    # Latest point per repo, same value for all three gauges in this fake;
    # the repo-less series drops out.
    assert facts.open_alerts_by_repo == {
        "pr_agent": {"code": 1, "leaks": 1, "errors": 1},
        "reid_basic": {"code": 0, "leaks": 0, "errors": 0},
    }
    assert facts.test_events_by_repo == {"reid_basic": 549}
    assert facts.coverage_by_repo == {"reid_basic": 71.7}


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
