"""The drift-demo scenario, declared as data.

Reference "today" for the dates below is 2026-07-01. Each ticket carries the
label ``drift-demo`` plus a stable per-ticket label ``ds-<slug>`` used for
idempotent find-or-create. Priorities are set after creation (priority is not
on the RC1 create screen). ``LEAD_UP`` is deliberately left in its default
"not started" status; every other status is applied via transition.

The ``slip`` on SLIP_UP is applied last so its changelog entry timestamps after
the downstream dates were set — that ordering is what rule 2 keys on.
"""

from __future__ import annotations

from dataclasses import dataclass

# All demo stories are parented under this dedicated epic to keep them grouped
# and trivially filterable/deletable — separate from the build epic (RC1-131).
EPIC_SUMMARY = "Drift Demo Data (seeded — safe to delete)"


@dataclass(frozen=True)
class Ticket:
    slug: str
    summary: str
    rule: str
    role: str
    priority: str = "Medium"
    due: str | None = None  # duedate at creation
    start: str | None = None  # customfield_10015 at creation
    status: str | None = None  # target status via transition; None = leave default
    blocks: tuple[str, ...] = ()  # slugs this ticket "blocks" (upstream -> downstream)
    # If set, duedate is edited to this value AFTER creation (rule-2 slip history).
    slip_due_to: str | None = None
    note: str = ""


TICKETS: list[Ticket] = [
    # --- Rule 1: timeline inversion (upstream due after downstream start/due) ---
    Ticket(
        slug="inv-up",
        summary="Finalize checkout API contract",
        rule="timeline_inversion",
        role="upstream",
        priority="High",
        due="2026-07-20",
        status="In Progress",
        blocks=("inv-down",),
        note="Upstream due 07-20 lands after downstream start 07-08 / due 07-16.",
    ),
    Ticket(
        slug="inv-down",
        summary="Integrate checkout API in web client",
        rule="timeline_inversion",
        role="downstream",
        priority="High",
        start="2026-07-08",
        due="2026-07-16",
    ),
    # --- Rule 2: unabsorbed slip (upstream due moved later, downstream unchanged) ---
    Ticket(
        slug="slip-up",
        summary="Vendor security review",
        rule="unabsorbed_slip",
        role="upstream",
        priority="High",
        due="2026-07-09",  # initial; slipped below
        slip_due_to="2026-07-23",  # 14-day slip, applied after downstream dates set
        status="In Progress",
        blocks=("slip-down",),
        note="Due slips 07-09 -> 07-23 (14d); downstream never reacts. Kept "
        "non-inverting (down start 07-24) so only rule 2 fires.",
    ),
    Ticket(
        slug="slip-down",
        summary="Production launch readiness sign-off",
        rule="unabsorbed_slip",
        role="downstream",
        priority="High",
        start="2026-07-24",
        due="2026-07-29",
    ),
    # --- Rule 3: lead-time risk (upstream not started, downstream starts soon) ---
    Ticket(
        slug="lead-up",
        summary="Provision staging Kubernetes cluster",
        rule="lead_time_risk",
        role="upstream",
        priority="High",
        status=None,  # left in default "Idea" (To Do category) = not started
        blocks=("lead-down",),
        note="Upstream not started; downstream starts 07-04 (~3 days out).",
    ),
    Ticket(
        slug="lead-down",
        summary="Execute staging load & soak tests",
        rule="lead_time_risk",
        role="downstream",
        priority="High",
        start="2026-07-04",
    ),
    # --- Rule 4: transitive risk (A blocks B blocks C; A moved to Blocked) ---
    Ticket(
        slug="trans-a",
        summary="Obtain DPA legal approval",
        rule="transitive_risk",
        role="blocked_source",
        priority="High",
        status="Blocked",
        blocks=("trans-b",),
        note="Transitive blocker of trans-c; entered Blocked.",
    ),
    Ticket(
        slug="trans-b",
        summary="Build customer data pipeline",
        rule="transitive_risk",
        role="chain_middle",
        priority="Medium",
        status="In Progress",
        blocks=("trans-c",),
    ),
    Ticket(
        slug="trans-c",
        summary="Ship analytics dashboard",
        rule="transitive_risk",
        role="downstream",
        priority="High",
        start="2026-07-30",
        due="2026-08-15",
    ),
    # --- Healthy control (no drift) ---
    Ticket(
        slug="ok-up",
        summary="Write API design doc",
        rule="healthy_control",
        role="upstream",
        priority="Medium",
        due="2026-07-04",
        status="In Progress",
        blocks=("ok-down",),
        note="Finishes before downstream starts; nothing slipped; started.",
    ),
    Ticket(
        slug="ok-down",
        summary="Implement API per design doc",
        rule="healthy_control",
        role="downstream",
        priority="Medium",
        start="2026-07-08",
        due="2026-07-18",
    ),
]

BY_SLUG: dict[str, Ticket] = {t.slug: t for t in TICKETS}


def links() -> list[tuple[str, str]]:
    """Return (blocker_slug, blocked_slug) pairs across the scenario."""
    out: list[tuple[str, str]] = []
    for t in TICKETS:
        for blocked in t.blocks:
            out.append((t.slug, blocked))
    return out


def expected_findings() -> list[dict]:
    """Human-readable ground truth: which rule should fire on which pair."""
    return [
        {"rule": "timeline_inversion", "on": "inv-down", "via": "inv-up"},
        {"rule": "unabsorbed_slip", "on": "slip-down", "via": "slip-up"},
        {"rule": "lead_time_risk", "on": "lead-down", "via": "lead-up"},
        {"rule": "transitive_risk", "on": "trans-c", "via": "trans-a (Blocked)"},
        {"rule": "none", "on": "ok-down", "via": "ok-up", "note": "negative control"},
    ]


# Sanity: every blocked slug must exist.
_missing = {b for _, b in links()} | {b for t in TICKETS for b in t.blocks}
assert _missing <= set(BY_SLUG), f"scenario references unknown slugs: {_missing - set(BY_SLUG)}"
del _missing
