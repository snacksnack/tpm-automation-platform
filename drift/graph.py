"""Dependency graph builder (RC1-136 [5/9]).

Turns a ProjectSnapshot into a directed graph the rules engine can walk: nodes
are issues (the Issue model is attached as node data), edges point from the
blocker (upstream) to the blocked ticket (downstream).

Defensive about real-world messiness: duplicate links collapse to one edge,
links pointing outside the project are dropped, and cycles (Jira allows them)
are broken deterministically so topological iteration is always possible. Each
of those is surfaced via a logged warning rather than an exception.
"""

from __future__ import annotations

import logging

import networkx as nx

from collectors.models import Issue, ProjectSnapshot

logger = logging.getLogger(__name__)


class DependencyGraph:
    def __init__(
        self,
        graph: nx.DiGraph,
        *,
        dropped_external: list[tuple[str, str]],
        broken_cycle_edges: list[tuple[str, str]],
    ):
        self._g = graph
        self.dropped_external = dropped_external
        self.broken_cycle_edges = broken_cycle_edges

    # --- construction -------------------------------------------------------
    @classmethod
    def from_snapshot(cls, snapshot: ProjectSnapshot) -> DependencyGraph:
        g: nx.DiGraph = nx.DiGraph()
        keys = {i.key for i in snapshot.issues}
        for issue in snapshot.issues:
            g.add_node(issue.key, issue=issue)

        dropped: list[tuple[str, str]] = []
        duplicates = 0
        for link in snapshot.links:
            edge = (link.upstream, link.downstream)
            if link.upstream not in keys or link.downstream not in keys:
                dropped.append(edge)
                continue
            if g.has_edge(*edge):
                duplicates += 1
                continue
            g.add_edge(link.upstream, link.downstream, link_type=link.link_type)

        if dropped:
            logger.warning(
                "dropped %d link(s) pointing outside the project: %s", len(dropped), dropped
            )
        if duplicates:
            logger.warning("ignored %d duplicate link(s)", duplicates)

        broken = _break_cycles(g)
        return cls(g, dropped_external=dropped, broken_cycle_edges=broken)

    # --- accessors the rules engine needs -----------------------------------
    def __contains__(self, key: str) -> bool:
        return key in self._g

    @property
    def graph(self) -> nx.DiGraph:
        return self._g

    def issue(self, key: str) -> Issue | None:
        node = self._g.nodes.get(key)
        return node.get("issue") if node else None

    def blocks(self, key: str) -> list[str]:
        """Direct downstream tickets that `key` blocks."""
        return sorted(self._g.successors(key)) if key in self._g else []

    def blocked_by(self, key: str) -> list[str]:
        """Direct upstream tickets that block `key`."""
        return sorted(self._g.predecessors(key)) if key in self._g else []

    def downstream_of(self, key: str, depth: int | None = None) -> list[str]:
        """All tickets reachable downstream of `key` (optionally within `depth` hops)."""
        if key not in self._g:
            return []
        if depth is None:
            return sorted(nx.descendants(self._g, key))
        reach = nx.single_source_shortest_path_length(self._g, key, cutoff=depth)
        return sorted(n for n in reach if n != key)

    def transitive_blockers(self, key: str) -> list[str]:
        """Every ticket that blocks `key`, directly or transitively (rule-4 input)."""
        return sorted(nx.ancestors(self._g, key)) if key in self._g else []

    def topological_order(self) -> list[str]:
        """Upstream-before-downstream ordering (the graph is always a DAG here)."""
        return list(nx.topological_sort(self._g))


def _break_cycles(g: nx.DiGraph) -> list[tuple[str, str]]:
    """Remove edges until the graph is acyclic, deterministically. Returns removed edges."""
    broken: list[tuple[str, str]] = []
    while True:
        try:
            cycle = nx.find_cycle(g)  # list of (u, v) edges
        except nx.NetworkXNoCycle:
            break
        # Deterministic choice: drop the lexicographically-largest edge in the cycle.
        edge = max((u, v) for u, v in cycle)
        g.remove_edge(*edge)
        broken.append(edge)
        logger.warning("broke dependency cycle by removing edge %s -> %s", *edge)
    return broken
