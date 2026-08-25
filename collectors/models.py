"""Typed models produced by the collector and reused across store/graph/rules.

These are the boundary between raw Jira JSON and the rest of the platform — no
raw dicts should leak past collect(). Shared with the future status-email v2.

RC1-301 adds the program-level snapshot: one `ProgramSnapshot` per run per
program, carrying the Jira project snapshot beside the other sources a KPI
tree names (the spend line, the eval store's run rows) and a health row per
source. A source that failed is *absent* and says so in `health`; it is never
an empty list pretending to be a measurement.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class DateChange(BaseModel):
    """A single scheduling-field change pulled from an issue's changelog.

    `field` is normalized to "duedate" or "start_date" (the raw Jira field ids
    are `duedate` and the start-date custom field). This is the key rule-2
    input: it says *when* an upstream date moved.
    """

    field: str
    from_date: date | None
    to_date: date | None
    changed_at: datetime


class Issue(BaseModel):
    key: str
    summary: str
    status: str
    status_category: str  # "To Do" | "In Progress" | "Done"
    priority: str | None = None
    assignee_id: str | None = None
    assignee_name: str | None = None
    due: date | None = None
    start: date | None = None
    # Scheduling-field changes for this issue, oldest first.
    date_changes: list[DateChange] = []
    # RC1-301: what the KPI tree reads that drift did not need.
    issue_type: str | None = None
    labels: list[str] = []
    points: float | None = None
    created: date | None = None
    parent: str | None = None

    @property
    def not_started(self) -> bool:
        """True when the issue is in the To Do category (rule-3 input)."""
        return self.status_category == "To Do"


class DependencyLink(BaseModel):
    """A directed "Blocks" edge: `upstream` blocks `downstream`."""

    upstream: str
    downstream: str
    link_type: str = "Blocks"


class ProjectSnapshot(BaseModel):
    project_key: str
    issues: list[Issue] = []
    links: list[DependencyLink] = []

    def issue(self, key: str) -> Issue | None:
        return next((i for i in self.issues if i.key == key), None)

    @property
    def by_key(self) -> dict[str, Issue]:
        return {i.key: i for i in self.issues}


# --- program snapshots (RC1-301) -------------------------------------------


class SpendRow(BaseModel):
    """One week of the cloud-spend line. `landed_on_day` is the sim-day the row
    became available (the simulator's "Monday after"); None for a real feed."""

    week: int
    week_start: date
    planned_usd: float
    actual_usd: float
    landed_on_day: int | None = None


class EvalRunRow(BaseModel):
    """One eval run as the KPI tree sees it: counts and cost, not the record."""

    run_id: str
    subject: str
    code_version: str
    model: str | None = None
    started_at: datetime
    cases: int
    passed: int
    errored: int
    cost_usd: float


class BillingRow(BaseModel):
    """One period of a real billing feed (RC1-308): dollars someone was billed,
    not dollars a price table computed.

    `metered` rows are daily buckets from a metered API (the Anthropic org
    cost report); `invoice` rows are whole billing periods (a Heroku monthly
    invoice). `period_end` is exclusive for metered rows, inclusive for
    invoices — each feed's own convention, preserved rather than papered over.
    """

    source: str = Field(description="anthropic-costs | heroku-invoices")
    period_start: date
    period_end: date
    amount_usd: float
    kind: Literal["metered", "invoice"] = "metered"


SourceStatus = Literal["ok", "missing", "error"]


class SourceHealth(BaseModel):
    """What happened when one source was read.

    `missing` is a source that answered with nothing — the query ran and
    returned no rows, the file is empty. `error` is a source that could not be
    read at all. The distinction matters downstream: a Jira query that returns
    zero issues under the program label *is* the week-7 source break, and an
    implementation that cannot tell it from "the project is empty" reports
    0 % scope change.
    """

    source: str = Field(description="jira | spend | eval-store | clock")
    status: SourceStatus
    count: int = 0
    detail: str = ""


class ProgramSnapshot(BaseModel):
    """Everything the KPI stages need for one program on one day, dated twice.

    `collected_at` is wall-clock; `sim_date` is the program's own calendar —
    the simulator's clock for the simulated program, the same date as
    `collected_at` for a real one. KPIs are computed against `sim_date`.
    """

    program_id: str
    collected_at: datetime
    sim_date: date
    sim_day: int | None = None
    jira: ProjectSnapshot | None = Field(
        default=None,
        description="None when the Jira source errored; empty when it answered nothing.",
    )
    spend: list[SpendRow] = []
    eval_runs: list[EvalRunRow] = []
    billing: list[BillingRow] = []
    health: list[SourceHealth] = []

    def source(self, name: str) -> SourceHealth | None:
        return next((h for h in self.health if h.source == name), None)

    @property
    def healthy(self) -> bool:
        return all(h.status == "ok" for h in self.health)
