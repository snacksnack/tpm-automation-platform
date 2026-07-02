"""Collector tests — fixture-driven, no live API calls (RC1-134 acceptance).

Fixtures under tests/fixtures/ are real RC1 responses captured by
scripts/capture_fixtures.py against the seeded drift-demo scenario.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from collectors.jira import (
    JiraCollector,
    dedupe_links,
    parse_changelog,
    parse_issue,
    parse_links,
)
from collectors.models import DependencyLink, Issue, ProjectSnapshot

FIX = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


@pytest.fixture
def search() -> dict:
    return _load("rc1_search_drift_demo.json")


@pytest.fixture
def slip_changelog() -> dict:
    return _load("rc1_changelog_slip.json")


@pytest.fixture
def nodates_changelog() -> dict:
    return _load("rc1_changelog_nodates.json")


def _by_summary(search: dict, summary: str) -> dict:
    return next(i for i in search["issues"] if i["fields"]["summary"] == summary)


def _key_by_summary(search: dict, summary: str) -> str:
    return _by_summary(search, summary)["key"]


# --------------------------------------------------------------------------- #
# parse_issue
# --------------------------------------------------------------------------- #
def test_parse_issue_full_fields(search: dict):
    raw = next(i for i in search["issues"] if i["fields"]["summary"] == "Vendor security review")
    issue = parse_issue(raw)
    assert isinstance(issue, Issue)
    assert issue.status == "In Progress"
    assert issue.status_category == "In Progress"
    assert issue.priority == "High"
    assert issue.due == date(2026, 7, 23)  # post-slip value
    assert issue.assignee_id  # populated


def test_parse_issue_without_dates_or_links_does_not_blow_up(search: dict):
    epic = next(
        i for i in search["issues"] if i["fields"]["summary"].startswith("Drift Demo Data")
    )
    issue = parse_issue(epic)
    assert issue.due is None
    assert issue.start is None
    assert parse_links(epic) == []  # no issuelinks -> no crash, empty list


def test_not_started_flag(search: dict):
    lead_up = _by_summary(search, "Provision staging Kubernetes cluster")
    blocked = _by_summary(search, "Obtain DPA legal approval")
    assert parse_issue(lead_up).not_started is True  # still "Idea" (To Do category)
    assert parse_issue(blocked).not_started is False  # Blocked is In Progress category


# --------------------------------------------------------------------------- #
# parse_links — the direction gotcha
# --------------------------------------------------------------------------- #
def test_links_count_and_direction(search: dict):
    links = dedupe_links([link for i in search["issues"] for link in parse_links(i)])
    assert len(links) == 6

    up = _key_by_summary(search, "Vendor security review")
    down = _key_by_summary(search, "Production launch readiness sign-off")
    # Upstream (blocker) -> downstream (blocked), not the reverse.
    assert DependencyLink(upstream=up, downstream=down) in links
    assert DependencyLink(upstream=down, downstream=up) not in links


def test_links_are_all_within_the_snapshot(search: dict):
    keys = {i["key"] for i in search["issues"]}
    links = dedupe_links([link for i in search["issues"] for link in parse_links(i)])
    for link in links:
        assert link.upstream in keys and link.downstream in keys


def test_transitive_chain_direction(search: dict):
    """A blocks B blocks C — both edges must point down the chain."""
    a = _key_by_summary(search, "Obtain DPA legal approval")
    b = _key_by_summary(search, "Build customer data pipeline")
    c = _key_by_summary(search, "Ship analytics dashboard")
    links = dedupe_links([link for i in search["issues"] for link in parse_links(i)])
    assert DependencyLink(upstream=a, downstream=b) in links
    assert DependencyLink(upstream=b, downstream=c) in links


def test_dedupe_links_collapses_both_ends():
    dupes = [
        DependencyLink(upstream="A", downstream="B"),
        DependencyLink(upstream="A", downstream="B"),
    ]
    assert dedupe_links(dupes) == [DependencyLink(upstream="A", downstream="B")]


# --------------------------------------------------------------------------- #
# parse_changelog
# --------------------------------------------------------------------------- #
def test_changelog_captures_duedate_slip(slip_changelog: dict):
    changes = parse_changelog(slip_changelog["values"])
    assert len(changes) == 1
    c = changes[0]
    assert c.field == "duedate"
    assert c.from_date == date(2026, 7, 9)
    assert c.to_date == date(2026, 7, 23)
    assert c.changed_at.year == 2026


def test_changelog_ignores_non_date_changes(nodates_changelog: dict):
    # This issue had priority + link changes but no scheduling-date change.
    assert parse_changelog(nodates_changelog["values"]) == []


# --------------------------------------------------------------------------- #
# collect() — full assembly offline via httpx.MockTransport (pagination too)
# --------------------------------------------------------------------------- #
def _mock_collector(search: dict, slip: dict, nodates: dict) -> JiraCollector:
    issues = search["issues"]
    slip_key = _key_by_summary(search, "Vendor security review")
    nodate_key = _key_by_summary(search, "Integrate checkout API in web client")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/search/jql"):
            body = json.loads(request.content)
            if not body.get("nextPageToken"):  # page 1 of 2 -> exercises pagination
                return httpx.Response(200, json={"issues": issues[:6], "nextPageToken": "PAGE2"})
            return httpx.Response(200, json={"issues": issues[6:], "isLast": True})
        if path.endswith("/changelog"):
            key = path.split("/issue/")[1].split("/changelog")[0]
            values = (
                slip["values"] if key == slip_key
                else nodates["values"] if key == nodate_key
                else []
            )
            return httpx.Response(200, json={"values": values, "isLast": True})
        return httpx.Response(404, json={"errorMessages": [f"unexpected {path}"]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return JiraCollector("https://example.test", client=client)


def test_collect_assembles_typed_snapshot(search, slip_changelog, nodates_changelog):
    collector = _mock_collector(search, slip_changelog, nodates_changelog)
    snap = collector.collect("RC1")

    assert isinstance(snap, ProjectSnapshot)
    assert len(snap.issues) == 12  # two pages merged
    assert len(snap.links) == 6
    # No raw dicts leak past collect().
    assert all(isinstance(i, Issue) for i in snap.issues)
    assert all(isinstance(link, DependencyLink) for link in snap.links)

    # Changelog wired onto the right issue.
    slip_issue = snap.issue(_key_by_summary(search, "Vendor security review"))
    assert slip_issue is not None
    assert [c.field for c in slip_issue.date_changes] == ["duedate"]

    # Issue with no scheduling changes has an empty changelog, no crash.
    epic = next(i for i in snap.issues if i.summary.startswith("Drift Demo Data"))
    assert epic.date_changes == []
