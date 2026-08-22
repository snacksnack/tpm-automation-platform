"""The Observability Platform GA program, declared as data (RC1-299).

Ten weeks, thirty-four stories in four workstreams, one sponsor commitment: a
GA date and a cloud-cost envelope. Sim-day 0 is kickoff (a Monday); GA is
committed for day 67, the Friday of week 10. Every planned date, actual
transition day, slip, flag and spend row is a number in this file, and
`state_at(day)` turns them into the state Jira must be in on that day.

Planted events, the ones the ground-truth ledger (RC1-300) tests the KPI
agent against — see docs/kpi/trees/simulated-program.md for which KPI must
see each, and by when:

    day 16-17  scope add       four stories, +16 points (+11.9 % of baseline)
    day 29     upstream slip   t-context due 27 -> 41; downstream dates untouched
    week 6     cost spike      actual 2.04x plan; the row lands on day 42
    day 43-47  source break    the program label is dropped from every story
                               (restored day 48, the TPM's "instrumentation fix")

Two things about dates. Jira cannot backdate a transition, so the simulator
never relies on the changelog — a status is *current state on a day*, and the
collector snapshots it (RC1-301). And the program's own dates are real
calendar dates (KICKOFF + day) because Jira date fields need them; sim-date
and wall-clock are deliberately different things.

Blocked work is a real status: PMA's workflow gained a global Blocked status
on 2026-08-22 (the Flagged/Impediment field was the stand-in before that), so
the KPI tree's "direct blocked" half reads exactly what it says.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta

PROJECT = "PMA"
PROGRAM_LABEL = "kpi-sim"  # what the collector keys on — dropped during the source break
SLUG_PREFIX = "ks-"  # what the simulator keys on — never dropped
EPIC_SLUG = "epic"
EPIC_SUMMARY = "Observability Platform GA (simulated — safe to delete)"
KICKOFF = date(2026, 9, 7)  # Monday
GA_DAY = 67  # Friday of week 10
LAST_DAY = 69  # the program's final sim-day (end of week 10)
WEEKS = 10
BASE_POINTS_PLAN_PER_WEEK = 13.5  # baseline points / 10 weeks; the plan rate

STATUS_TODO = "To Do"
STATUS_IN_PROGRESS = "In Progress"
STATUS_REVIEW = "CODE REVIEW"
STATUS_BLOCKED = "Blocked"
STATUS_DONE = "Done"

OWNERS = ("Priya", "Marcus", "Lena", "Tomás", "Aisha")


def sim_date(day: int) -> date:
    return KICKOFF + timedelta(days=day)


def week_of(day: int) -> int:
    """1-based program week containing `day`."""
    return day // 7 + 1


def slug_label(slug: str) -> str:
    return f"{SLUG_PREFIX}{slug}"


@dataclass(frozen=True)
class Story:
    slug: str
    summary: str
    workstream: str  # tracing | slo | alerting | platform
    owner: str
    points: int
    start: int  # planned start day
    due: int  # planned due day, as set at creation
    done: int  # day it reaches Done
    started: int | None = None  # day it moves to In Progress; default = start
    created: int = 0  # day the story appears (scope adds are created later)
    blocks: tuple[str, ...] = ()  # slugs this story blocks (upstream -> downstream)
    slip: tuple[int, int] | None = None  # (day applied, new due day)
    blocked: tuple[int, int] | None = None  # [from, to) days in the Blocked status
    ga_blocking: bool = False  # the GA sign-off root; chains into it are the critical path
    note: str = ""

    @property
    def started_day(self) -> int:
        return self.start if self.started is None else self.started

    def due_on(self, day: int) -> int:
        if self.slip and day >= self.slip[0]:
            return self.slip[1]
        return self.due

    def status_on(self, day: int) -> str:
        if day >= self.done:
            return STATUS_DONE
        if self.blocked_on(day):
            return STATUS_BLOCKED
        if day >= self.started_day:
            # A day in review before Done, only for stories that ran long enough
            # to have one. Deterministic, and it gives the snapshot diff a
            # second transition to see.
            if day >= self.done - 1 and self.done - self.started_day >= 4:
                return STATUS_REVIEW
            return STATUS_IN_PROGRESS
        return STATUS_TODO

    def blocked_on(self, day: int) -> bool:
        return bool(self.blocked) and self.blocked[0] <= day < self.blocked[1]


# fmt: off
STORIES: tuple[Story, ...] = (
    # --- platform: the pipeline everything else runs on, and the GA root ---
    Story("p-infra", "Telemetry pipeline infra: ingest, storage, query", "platform", "Tomás",
          5, start=0, due=5, done=5, blocks=("t-collector",)),
    Story("p-security", "Security review of telemetry data handling", "platform", "Tomás",
          3, start=20, due=30, done=33, blocked=(27, 33),
          note="Waits on the security team from day 27; Blocked until it lands."),
    Story("p-cost", "Cost guardrails: sampling and retention caps", "platform", "Priya",
          3, start=36, due=42, done=44,
          note="The response to the week-6 spend spike; lands after the spike row."),
    Story("p-loadtest", "Load test the telemetry pipeline at GA volume", "platform", "Tomás",
          5, start=45, due=52, done=53, blocks=("p-ga",)),
    Story("p-docs", "GA documentation and onboarding guide", "platform", "Lena",
          3, start=55, due=61, done=61),
    Story("p-ga", "GA readiness sign-off", "platform", "Tomás",
          2, start=62, due=GA_DAY, done=GA_DAY, ga_blocking=True),

    # --- tracing ---
    Story("t-sdk", "Adopt tracing SDK in the service template", "tracing", "Priya",
          5, start=0, due=6, done=5),
    Story("t-collector", "Deploy trace collector with head sampling", "tracing", "Marcus",
          8, start=8, due=14, done=14, blocks=("s-ingest",)),
    Story("t-gateway", "Instrument the API gateway", "tracing", "Priya",
          5, start=2, due=9, done=9),
    Story("t-checkout", "Instrument the checkout service", "tracing", "Priya",
          5, start=7, due=13, done=14),
    Story("t-search", "Instrument the search service", "tracing", "Marcus",
          5, start=7, due=16, done=17),
    Story("t-storage", "Trace storage retention policy", "tracing", "Marcus",
          3, start=13, due=20, done=19),
    Story("t-context", "Context propagation across async queues", "tracing", "Priya",
          8, start=14, due=27, done=41, started=16, blocks=("s-latency",),
          slip=(29, 41),
          note="The planted slip: due 27 -> 41 on day 29. Downstream s-latency keeps "
               "its start of 30, so slack goes from +3 to -11 the same day."),
    Story("t-sampling", "Tail-based sampling tuning", "tracing", "Marcus",
          5, start=28, due=34, done=36),

    # --- SLO dashboards ---
    Story("s-targets", "Define SLO targets for the three tier-1 services", "slo", "Lena",
          3, start=7, due=13, done=12, blocks=("s-burn",)),
    Story("s-ingest", "Metrics ingest pipeline for SLIs", "slo", "Tomás",
          8, start=17, due=26, done=26, blocks=("s-dash-search", "s-dash-api", "x-slo-mobile")),
    Story("s-latency", "Latency SLIs from trace data", "slo", "Lena",
          5, start=30, due=36, done=47, started=42, blocked=(30, 42),
          blocks=("s-dash-checkout",),
          note="Cannot start until t-context lands; Blocked from its planned start "
               "until it actually starts."),
    Story("s-dash-search", "Search SLO dashboard", "slo", "Lena",
          5, start=29, due=35, done=35, blocks=("a-search-alerts",)),
    Story("s-dash-api", "API gateway SLO dashboard", "slo", "Tomás",
          5, start=29, due=37, done=37),
    Story("s-dash-checkout", "Checkout SLO dashboard", "slo", "Lena",
          5, start=39, due=45, done=53, started=48, blocks=("a-checkout-alerts",)),
    Story("s-burn", "Error-budget burn-rate computation", "slo", "Tomás",
          5, start=32, due=40, done=40, blocks=("a-burn-alerts",)),
    Story("s-review", "SLO review with service owners", "slo", "Lena",
          2, start=41, due=45, done=45),

    # --- alerting ---
    Story("a-schema", "Structured alert schema", "alerting", "Aisha",
          3, start=14, due=20, done=20, blocks=("a-routing",)),
    Story("a-routing", "Alert routing to the on-call rotation", "alerting", "Aisha",
          5, start=23, due=29, done=29, blocks=("a-pager",)),
    Story("a-pager", "Pager integration and escalation policy", "alerting", "Marcus",
          5, start=32, due=39, done=39),
    Story("a-search-alerts", "Search SLO alerts", "alerting", "Aisha",
          3, start=38, due=43, done=43),
    Story("a-burn-alerts", "Burn-rate alert rules", "alerting", "Aisha",
          5, start=43, due=49, done=49),
    Story("a-checkout-alerts", "Checkout SLO alerts", "alerting", "Aisha",
          5, start=48, due=55, done=59, started=54, blocks=("p-ga",)),
    Story("a-runbooks", "Runbook links on every alert", "alerting", "Marcus",
          3, start=50, due=56, done=56, blocks=("p-ga",)),
    Story("a-noise", "Alert noise review: dedupe and grouping", "alerting", "Aisha",
          3, start=57, due=62, done=62),

    # --- week-3 scope add: the mobile BFF was not in the committed scope ---
    Story("x-mobile", "Instrument the mobile BFF", "tracing", "Priya",
          5, start=18, due=27, done=29, created=16, blocks=("x-slo-mobile",),
          note="Scope add, day 16."),
    Story("x-slo-mobile", "Mobile BFF SLO dashboard", "slo", "Lena",
          5, start=34, due=41, done=43, created=16, blocks=("x-alerts-mobile",),
          note="Scope add, day 16."),
    Story("x-alerts-mobile", "Mobile BFF alerts", "alerting", "Aisha",
          3, start=44, due=50, done=52, created=16, note="Scope add, day 16."),
    Story("x-audit", "Audit-log export of alert history", "alerting", "Marcus",
          3, start=50, due=57, done=59, created=17, note="Scope add, day 17."),
)
# fmt: on

BY_SLUG: dict[str, Story] = {s.slug: s for s in STORIES}


# --- the cloud spend line ----------------------------------------------------------

SPEND_PLAN_USD = 1200.0  # per week, flat
#: Actual spend per week 1..10. Week 6 is the planted spike (2.04x plan).
SPEND_ACTUAL_USD = (1150.0, 1240.0, 1190.0, 1310.0, 1280.0, 2450.0, 1390.0, 1320.0, 1270.0, 1260.0)
SPIKE_WEEK = 6


@dataclass(frozen=True)
class SpendRow:
    week: int
    planned_usd: float
    actual_usd: float
    lands_on_day: int  # the Monday after the week: available from this sim-day


def spend_rows(day: int) -> list[SpendRow]:
    """Rows that have landed by `day`. Week w covers days 7(w-1)..7w-1 and lands on 7w."""
    return [
        SpendRow(w, SPEND_PLAN_USD, SPEND_ACTUAL_USD[w - 1], 7 * w)
        for w in range(1, WEEKS + 1)
        if 7 * w <= day
    ]


# --- planted events ------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    id: str
    day: int  # first sim-day the event is observable in a snapshot
    until: int | None  # last sim-day it is active (None = through the end)
    detail: str
    must_move: tuple[str, ...]  # KPI ids from the adopted tree that must react


EVENTS: tuple[Event, ...] = (
    Event(
        "scope-add", 16, 17,
        "Four mobile-BFF stories created: +16 points on a 135-point baseline (+11.9 %).",
        ("scope-change-pct", "forecast-slip-days"),
    ),
    Event(
        "upstream-slip", 29, None,
        "t-context due 27 -> 41 (+14). Downstream s-latency keeps start 30: slack +3 -> -11.",
        ("critical-path-slack-days", "blocked-share-pct", "forecast-slip-days"),
    ),
    Event(
        "cost-spike", 42, None,
        "Week-6 actual $2,450 vs $1,200 plan (2.04x); the row lands on day 42.",
        ("weekly-spend-burn-ratio", "cost-vs-envelope"),
    ),
    Event(
        "source-break", 43, 47,
        f"The {PROGRAM_LABEL} label is removed from every story; restored on day 48.",
        ("forecast-slip-days", "scope-change-pct", "critical-path-slack-days",
         "blocked-share-pct"),  # all Jira-sourced KPIs go stale; cost KPIs do not
    ),
)
SOURCE_BREAK = EVENTS[3]


def active_events(day: int) -> list[Event]:
    return [e for e in EVENTS if e.day <= day and (e.until is None or day <= e.until)]


def source_broken_on(day: int) -> bool:
    return SOURCE_BREAK.day <= day <= (SOURCE_BREAK.until or LAST_DAY)


# --- state on a day ---------------------------------------------------------------------


@dataclass(frozen=True)
class IssueState:
    slug: str
    summary: str
    status: str
    start: date
    due: date
    points: int
    labels: frozenset[str]
    blocks: tuple[str, ...]  # slugs, only those that also exist on this day
    owner: str
    workstream: str
    created_day: int


@dataclass(frozen=True)
class ProgramState:
    day: int
    date: date
    epic_summary: str
    epic_due: date
    epic_labels: frozenset[str]
    issues: dict[str, IssueState] = field(default_factory=dict)
    spend: list[SpendRow] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)

    @property
    def points_total(self) -> int:
        return sum(i.points for i in self.issues.values())

    @property
    def points_done(self) -> int:
        return sum(i.points for i in self.issues.values() if i.status == STATUS_DONE)


def owner_label(owner: str) -> str:
    """Jira labels are ASCII-safe here: Tomás -> own-tomas."""
    ascii_name = unicodedata.normalize("NFKD", owner).encode("ascii", "ignore").decode()
    return f"own-{ascii_name.lower()}"


def labels_on(story: Story, day: int) -> frozenset[str]:
    labels = {slug_label(story.slug), f"ws-{story.workstream}", owner_label(story.owner)}
    if not source_broken_on(day):
        labels.add(PROGRAM_LABEL)
    return frozenset(labels)


def state_at(day: int) -> ProgramState:
    """Everything Jira should show on sim-day `day`. Pure; the ledger derives from it."""
    if not 0 <= day <= LAST_DAY:
        raise ValueError(f"day {day} is outside the program (0..{LAST_DAY})")
    present = {s.slug for s in STORIES if s.created <= day}
    issues = {
        s.slug: IssueState(
            slug=s.slug,
            summary=s.summary,
            status=s.status_on(day),
            start=sim_date(s.start),
            due=sim_date(s.due_on(day)),
            points=s.points,
            labels=labels_on(s, day),
            blocks=tuple(b for b in s.blocks if b in present),
            owner=s.owner,
            workstream=s.workstream,
            created_day=s.created,
        )
        for s in STORIES
        if s.slug in present
    }
    epic_labels = {slug_label(EPIC_SLUG)}
    if not source_broken_on(day):
        epic_labels.add(PROGRAM_LABEL)
    return ProgramState(
        day=day,
        date=sim_date(day),
        epic_summary=EPIC_SUMMARY,
        epic_due=sim_date(GA_DAY),
        epic_labels=frozenset(epic_labels),
        issues=issues,
        spend=spend_rows(day),
        events=active_events(day),
    )


def links() -> list[tuple[str, str]]:
    """(blocker, blocked) over the whole scenario, for graph checks."""
    return [(s.slug, b) for s in STORIES for b in s.blocks]


def baseline_points() -> int:
    return sum(s.points for s in STORIES if s.created == 0)


def added_points() -> int:
    return sum(s.points for s in STORIES if s.created > 0)


def description_for(story: Story) -> str:
    return (
        f"Owner: {story.owner} · Workstream: {story.workstream} · {story.points} pts. "
        f"Simulated program story (RC1-299); safe to delete."
        + (f" {story.note}" if story.note else "")
    )
