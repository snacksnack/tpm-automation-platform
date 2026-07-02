"""Dependency graph tests (RC1-136 acceptance)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from collectors.jira import dedupe_links, parse_issue, parse_links
from collectors.models import DependencyLink, Issue, ProjectSnapshot
from drift.graph import DependencyGraph

FIX = Path(__file__).parent / "fixtures"


def _issue(key: str) -> Issue:
    return Issue(key=key, summary=f"Summary {key}", status="To Do", status_category="To Do")


def _snapshot(keys: list[str], links: list[tuple[str, str]]) -> ProjectSnapshot:
    return ProjectSnapshot(
        project_key="RC1",
        issues=[_issue(k) for k in keys],
        links=[DependencyLink(upstream=u, downstream=d) for u, d in links],
    )


# --------------------------------------------------------------------------- #
# Direction — the thing that's easy to get backwards
# --------------------------------------------------------------------------- #
def test_edge_direction_upstream_to_downstream():
    g = DependencyGraph.from_snapshot(_snapshot(["A", "B"], [("A", "B")]))
    assert g.graph.has_edge("A", "B")
    assert not g.graph.has_edge("B", "A")
    assert g.blocks("A") == ["B"]
    assert g.blocked_by("B") == ["A"]
    assert g.downstream_of("A") == ["B"]
    assert g.transitive_blockers("B") == ["A"]


def test_node_carries_the_issue():
    g = DependencyGraph.from_snapshot(_snapshot(["A"], []))
    assert isinstance(g.issue("A"), Issue)
    assert g.issue("A").key == "A"
    assert g.issue("missing") is None


# --------------------------------------------------------------------------- #
# Transitive chain (3-deep)
# --------------------------------------------------------------------------- #
def test_three_deep_transitive_chain():
    g = DependencyGraph.from_snapshot(
        _snapshot(["A", "B", "C"], [("A", "B"), ("B", "C")])
    )
    assert g.transitive_blockers("C") == ["A", "B"]
    assert g.downstream_of("A") == ["B", "C"]
    assert g.downstream_of("A", depth=1) == ["B"]  # depth-limited stops at direct
    order = g.topological_order()
    assert order.index("A") < order.index("B") < order.index("C")


# --------------------------------------------------------------------------- #
# Messy real-world input
# --------------------------------------------------------------------------- #
def test_duplicate_links_collapse_to_one_edge(caplog):
    with caplog.at_level(logging.WARNING):
        g = DependencyGraph.from_snapshot(_snapshot(["A", "B"], [("A", "B"), ("A", "B")]))
    assert g.graph.number_of_edges() == 1
    assert "duplicate" in caplog.text


def test_links_outside_project_are_dropped(caplog):
    with caplog.at_level(logging.WARNING):
        g = DependencyGraph.from_snapshot(_snapshot(["A", "B"], [("A", "B"), ("A", "ZZZ")]))
    assert g.graph.number_of_edges() == 1
    assert g.dropped_external == [("A", "ZZZ")]
    assert "outside the project" in caplog.text


def test_cycle_is_broken_and_logged(caplog):
    with caplog.at_level(logging.WARNING):
        g = DependencyGraph.from_snapshot(
            _snapshot(["A", "B", "C"], [("A", "B"), ("B", "C"), ("C", "A")])
        )
    # Build did not raise; graph is now acyclic so topo sort works.
    assert len(g.broken_cycle_edges) == 1
    assert g.topological_order()  # no exception
    assert "cycle" in caplog.text


# --------------------------------------------------------------------------- #
# Against real captured RC1 data (the seeded transitive chain)
# --------------------------------------------------------------------------- #
def _snapshot_from_fixture() -> ProjectSnapshot:
    raw = json.loads((FIX / "rc1_search_drift_demo.json").read_text())["issues"]
    return ProjectSnapshot(
        project_key="RC1",
        issues=[parse_issue(i) for i in raw],
        links=dedupe_links([link for i in raw for link in parse_links(i)]),
    )


def _key(snap: ProjectSnapshot, summary: str) -> str:
    return next(i.key for i in snap.issues if i.summary == summary)


def test_real_transitive_chain_from_fixture():
    snap = _snapshot_from_fixture()
    g = DependencyGraph.from_snapshot(snap)
    a = _key(snap, "Obtain DPA legal approval")
    b = _key(snap, "Build customer data pipeline")
    c = _key(snap, "Ship analytics dashboard")
    assert g.transitive_blockers(c) == sorted([a, b])
    assert g.downstream_of(a) == sorted([b, c])
    assert g.broken_cycle_edges == []  # the seeded scenario is a clean DAG
