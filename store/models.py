"""Finding model — a single drift detection, persisted by the store.

The rules engine ([6/9]) produces these; the store persists them and stamps
`first_seen_run` so downstream notification ([8/9]) can tell new findings from
ones that have been open since a prior run.

Identity is (rule_type, upstream, downstream): the same drift on the same pair
maps to the same finding across runs, which is what dedup / first-seen rely on.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class RunInfo(BaseModel):
    """A recorded run, as a reader sees it.

    Carried on every read-only findings response so a caller can state which run
    the data came from and when — findings describe the last *stored* run, not
    live Jira, and a response that omits that invites a reader to assume it is
    current (RC1-244).
    """

    run_id: int
    project_key: str
    created_at: datetime


class Finding(BaseModel):
    rule_type: str
    downstream: str  # the affected ticket
    upstream: str | None = None  # the cause ticket (some rules have none)
    severity: float = 0.0
    severity_bucket: str = ""  # "red" | "yellow" | "white"
    detail: str = ""

    # Populated by the store on save/load; None before persistence.
    run_id: int | None = None
    first_seen_run: int | None = None

    @property
    def identity(self) -> tuple[str, str | None, str]:
        return (self.rule_type, self.upstream, self.downstream)

    @property
    def is_new(self) -> bool:
        """True when this finding was first seen in its own run (not carried over)."""
        return self.run_id is not None and self.run_id == self.first_seen_run
