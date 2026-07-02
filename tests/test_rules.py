"""Drift rules tests (RC1-137 acceptance).

Every rule fires on its positive case and stays silent on the healthy control;
an end-to-end pass over the seeded fixture fires all four; first_seen_run and
rule-4 re-alert suppression are exercised through the real store.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from collectors.jira import dedupe_links, parse_changelog, parse_issue, parse_links
from collectors.models import DateChange, DependencyLink, Issue, ProjectSnapshot
from drift.graph import DependencyGraph
from drift.rules import (
    detect_all,
    lead_time_risk,
    timeline_inversion,
    transitive_risk,
    unabsorbed_slip,
)
from store.snapshot_store import SnapshotStore

FIX = Path(__file__).parent / "fixtures"
TODAY = date(2026, 7, 1)


def _issue(
    key: str, *, status: str = "In Progress", category: str = "In Progress",
    priority: str = "High", due: date | None = None, start: date | None = None,
    changes: list[DateChange] | None = None,
) -> Issue:
    return Issue(
        key=key, summary=f"S {key}", status=status, status_category=category,
        priority=priority, due=due, start=start, date_changes=changes or [],
    )


def _graph(
    issues: list[Issue], links: list[tuple[str, str]]
) -> tuple[DependencyGraph, ProjectSnapshot]:
    snap = ProjectSnapshot(
        project_key="RC1", issues=issues,
        links=[DependencyLink(upstream=u, downstream=d) for u, d in links],
    )
    return DependencyGraph.from_snapshot(snap), snap


def _slip(from_d: date, to_d: date, when: datetime) -> DateChange:
    return DateChange(field="duedate", from_date=from_d, to_date=to_d, changed_at=when)


# --------------------------------------------------------------------------- #
# Rule 1 — timeline inversion
# --------------------------------------------------------------------------- #
def test_timeline_inversion_fires():
    g, snap = _graph(
        [_issue("U", due=date(2026, 7, 20)), _issue("D", start=date(2026, 7, 8))],
        [("U", "D")],
    )
    findings = timeline_inversion(g, snap, today=TODAY)
    assert len(findings) == 1
    assert findings[0].downstream == "D" and findings[0].severity > 0


def test_timeline_inversion_silent_when_healthy():
    g, snap = _graph(
        [_issue("U", due=date(2026, 7, 4)), _issue("D", start=date(2026, 7, 8))],
        [("U", "D")],
    )
    assert timeline_inversion(g, snap, today=TODAY) == []


# --------------------------------------------------------------------------- #
# Rule 2 — unabsorbed slip
# --------------------------------------------------------------------------- #
def test_unabsorbed_slip_fires():
    slipped = _slip(date(2026, 7, 9), date(2026, 7, 23), datetime(2026, 7, 2, 8, tzinfo=UTC))
    g, snap = _graph(
        [_issue("U", due=date(2026, 7, 23), changes=[slipped]),
         _issue("D", start=date(2026, 7, 24), due=date(2026, 7, 29))],
        [("U", "D")],
    )
    findings = unabsorbed_slip(g, snap, today=TODAY)
    assert len(findings) == 1
    assert "slipped" in findings[0].detail


def test_unabsorbed_slip_silent_when_downstream_reacted():
    slipped = _slip(date(2026, 7, 9), date(2026, 7, 23), datetime(2026, 7, 2, 8, tzinfo=UTC))
    reacted = _slip(date(2026, 7, 24), date(2026, 8, 1), datetime(2026, 7, 2, 9, tzinfo=UTC))
    g, snap = _graph(
        [_issue("U", due=date(2026, 7, 23), changes=[slipped]),
         _issue("D", start=date(2026, 8, 1), changes=[reacted])],
        [("U", "D")],
    )
    assert unabsorbed_slip(g, snap, today=TODAY) == []  # downstream moved after the slip


def test_unabsorbed_slip_silent_when_slip_too_small():
    tiny = _slip(date(2026, 7, 9), date(2026, 7, 10), datetime(2026, 7, 2, 8, tzinfo=UTC))
    g, snap = _graph(
        [_issue("U", changes=[tiny]), _issue("D", start=date(2026, 7, 24))], [("U", "D")]
    )
    assert unabsorbed_slip(g, snap, today=TODAY) == []


# --------------------------------------------------------------------------- #
# Rule 3 — lead-time risk
# --------------------------------------------------------------------------- #
def test_lead_time_risk_fires():
    g, snap = _graph(
        [_issue("U", status="Idea", category="To Do"), _issue("D", start=date(2026, 7, 3))],
        [("U", "D")],
    )
    findings = lead_time_risk(g, snap, today=TODAY)
    assert len(findings) == 1


def test_lead_time_risk_silent_when_upstream_started():
    g, snap = _graph(
        [_issue("U", status="In Progress"), _issue("D", start=date(2026, 7, 3))],
        [("U", "D")],
    )
    assert lead_time_risk(g, snap, today=TODAY) == []


def test_lead_time_risk_silent_when_start_far_away():
    g, snap = _graph(
        [_issue("U", status="Idea", category="To Do"), _issue("D", start=date(2026, 9, 1))],
        [("U", "D")],
    )
    assert lead_time_risk(g, snap, today=TODAY) == []


# --------------------------------------------------------------------------- #
# Rule 4 — transitive risk
# --------------------------------------------------------------------------- #
def _chain():
    return _graph(
        [
            _issue("A", status="Blocked", priority="High"),
            _issue("B", status="In Progress", priority="Medium"),
            _issue("C", status="In Progress", priority="High", start=date(2026, 7, 30)),
        ],
        [("A", "B"), ("B", "C")],
    )


def test_transitive_risk_fires_on_first_run():
    g, snap = _chain()
    findings = transitive_risk(g, snap, previous=None, today=TODAY)
    downstreams = {f.downstream for f in findings}
    assert downstreams == {"B", "C"}  # both are downstream of the Blocked A


def test_transitive_risk_suppressed_when_already_blocked_last_run():
    g, snap = _chain()
    assert transitive_risk(g, snap, previous=snap, today=TODAY) == []  # A was Blocked last run too


def test_transitive_risk_silent_when_blocker_not_blocked():
    g, snap = _graph(
        [_issue("A", status="In Progress"), _issue("B"), _issue("C")],
        [("A", "B"), ("B", "C")],
    )
    assert transitive_risk(g, snap, previous=None, today=TODAY) == []


# --------------------------------------------------------------------------- #
# End-to-end over the seeded fixture
# --------------------------------------------------------------------------- #
def _fixture_snapshot() -> tuple[DependencyGraph, ProjectSnapshot]:
    raw = json.loads((FIX / "rc1_search_drift_demo.json").read_text())["issues"]
    issues = [parse_issue(i) for i in raw]
    slip_values = json.loads((FIX / "rc1_changelog_slip.json").read_text())["values"]
    for issue in issues:
        if issue.summary == "Vendor security review":  # attach the real slip changelog
            issue.date_changes = parse_changelog(slip_values)
    snap = ProjectSnapshot(
        project_key="RC1", issues=issues,
        links=dedupe_links([link for i in raw for link in parse_links(i)]),
    )
    return DependencyGraph.from_snapshot(snap), snap


def test_all_four_rules_fire_and_control_is_silent():
    g, snap = _fixture_snapshot()
    findings = detect_all(g, snap, previous=None, today=TODAY)

    assert {f.rule_type for f in findings} == {
        "timeline_inversion", "unabsorbed_slip", "lead_time_risk", "transitive_risk",
    }
    # Sorted most-severe first, and at least one red-bucket finding.
    assert findings == sorted(findings, key=lambda f: f.severity, reverse=True)
    assert any(f.severity_bucket == "red" for f in findings)

    # Healthy control (ok-up / ok-down) never appears in any finding.
    ok_up = next(i.key for i in snap.issues if i.summary == "Write API design doc")
    ok_down = next(i.key for i in snap.issues if i.summary == "Implement API per design doc")
    touched = {f.upstream for f in findings} | {f.downstream for f in findings}
    assert ok_up not in touched and ok_down not in touched


def test_first_seen_and_rule4_suppression_through_store():
    g, snap = _fixture_snapshot()
    with SnapshotStore(":memory:") as store:
        r1 = store.create_run("RC1")
        saved1 = store.save_findings(r1, detect_all(g, snap, previous=None, today=TODAY))
        assert all(f.is_new for f in saved1)

        r2 = store.create_run("RC1")
        run2 = detect_all(g, snap, previous=snap, today=TODAY)  # prev has blocker already Blocked
        saved2 = store.save_findings(r2, run2)

        # Rule 4 no longer fires (no re-alert); persistent rules keep first_seen == r1.
        assert "transitive_risk" not in {f.rule_type for f in run2}
        assert saved2 and all(f.first_seen_run == r1 and not f.is_new for f in saved2)
