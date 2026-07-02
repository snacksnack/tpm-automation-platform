"""Typed models produced by the collector and reused across store/graph/rules.

These are the boundary between raw Jira JSON and the rest of the platform — no
raw dicts should leak past collect(). Shared with the future status-email v2.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel


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
