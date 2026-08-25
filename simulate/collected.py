"""What the collector would store on a sim-day, rendered from the scenario (RC1-310).

The mirror of `ledger.snapshot_from_collected`: that adapter reads a stored
`ProgramSnapshot` into the ledger's shape, this renders `scenario.state_at(day)`
into the collector's shape with no Jira in the loop. It exists for the
`kpi-ledger` eval — the track stage's measures (`kpi/measures.py`) read
collected snapshots, the ledger derives from the scenario, and the eval needs
the two sides to meet on data neither computed for the other.

Faithful to `collectors.program.collect_program` where the KPI formulas can
see the difference: only issues carrying the program label appear (the source
break is an emptied answer, health `missing`, not an error), the epic rides
along for its committed GA date, links exist only between visible issues, and
spend rows land the Monday after their week.
"""

from __future__ import annotations

from datetime import UTC, datetime

from collectors.models import (
    DependencyLink,
    Issue,
    ProgramSnapshot,
    ProjectSnapshot,
    SourceHealth,
    SpendRow,
)
from simulate import scenario

#: Jira's three status categories, from the scenario's status names. Blocked
#: and CODE REVIEW sit in "In Progress", exactly as PMA's workflow has them.
_CATEGORY = {scenario.STATUS_TODO: "To Do", scenario.STATUS_DONE: "Done"}


def _key(slug: str) -> str:
    return f"{scenario.PROJECT}-{slug}"


def program_snapshot(day: int, *, collected_at: datetime | None = None) -> ProgramSnapshot:
    """One day of the simulated program as the collector would have stored it."""
    state = scenario.state_at(day)
    issues: list[Issue] = []
    if scenario.PROGRAM_LABEL in state.epic_labels:
        issues.append(
            Issue(
                key=_key(scenario.EPIC_SLUG),
                summary=state.epic_summary,
                status="In Progress",
                status_category="In Progress",
                due=state.epic_due,
                issue_type="Epic",
                labels=sorted(state.epic_labels),
            )
        )
    visible = {
        slug: i for slug, i in state.issues.items() if scenario.PROGRAM_LABEL in i.labels
    }
    for slug, i in sorted(visible.items()):
        issues.append(
            Issue(
                key=_key(slug),
                summary=i.summary,
                status=i.status,
                status_category=_CATEGORY.get(i.status, "In Progress"),
                due=i.due,
                start=i.start,
                issue_type="Story",
                labels=sorted(i.labels),
                points=float(i.points),
                created=scenario.sim_date(i.created_day),
                parent=_key(scenario.EPIC_SLUG),
            )
        )
    links = [
        DependencyLink(upstream=_key(slug), downstream=_key(down))
        for slug, i in sorted(visible.items())
        for down in i.blocks
        if down in visible
    ]
    spend = [
        SpendRow(
            week=r.week,
            week_start=scenario.sim_date(7 * (r.week - 1)),
            planned_usd=r.planned_usd,
            actual_usd=r.actual_usd,
            landed_on_day=r.lands_on_day,
        )
        for r in state.spend
    ]
    n = len(issues)
    health = [
        SourceHealth(source="clock", status="ok", count=1, detail=f"day {day}"),
        SourceHealth(
            source="jira",
            status="ok" if n else "missing",
            count=n,
            detail=f"{n} issue(s) under {scenario.PROGRAM_LABEL!r}"
            if n
            else f"query returned no issues under {scenario.PROGRAM_LABEL!r}",
        ),
        SourceHealth(
            source="spend",
            status="ok" if spend else "missing",
            count=len(spend),
            detail=f"{len(spend)} week(s) landed" if spend else "no weeks landed yet",
        ),
    ]
    return ProgramSnapshot(
        program_id="simulated-program",
        collected_at=collected_at or datetime.now(UTC),
        sim_date=state.date,
        sim_day=day,
        jira=ProjectSnapshot(project_key=scenario.PROJECT, issues=issues, links=links),
        spend=spend,
        health=health,
    )
