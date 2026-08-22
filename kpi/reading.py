"""A KPI reading — what the track stage emits, and what the ledger expects (RC1-300).

One shape on both sides of the diff. The ground-truth ledger
(`simulate/ledger.py`) is a `Reading` per KPI per sim-day plus a tolerance;
the track stage (RC1-305) produces a `Reading` per KPI per snapshot; the
`kpi-ledger` eval subject compares the two field by field. Agreeing on the
shape here, before either side exists in anger, is what makes "the agent's
output is diffed against the ledger" a one-line comparison rather than an
adapter.

`state` is the rubric's honesty rule as a type. A KPI whose source has gone
away reads `broken` and carries the last good value *with its date*; a KPI
whose value is older than its `stale_after` — or that has never had one —
reads `stale`. Neither is ever zero. `tripped` is the so-what threshold:
the reading is not only a number but whether the decision attached to the
number is now live.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

KpiState = Literal["ok", "stale", "broken"]


class Reading(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kpi_id: str
    sim_date: date = Field(description="The day the reading is *for*.")
    value: float | None = Field(
        description="None when no value exists (never 0 for 'unknown').",
    )
    state: KpiState = "ok"
    tripped: bool = Field(
        default=False,
        description="The KPI's so-what threshold is crossed on this reading.",
    )
    as_of: date | None = Field(
        default=None,
        description="Date of the data behind `value`; earlier than sim_date when carried.",
    )
    reason: str | None = Field(
        default=None, description="Why the state is not ok. Required when it is not."
    )
    detail: str = Field(default="", description="The working: inputs, chain, halves.")

    @model_validator(mode="after")
    def _not_ok_needs_a_reason(self) -> Reading:
        if self.state != "ok" and not self.reason:
            raise ValueError(f"{self.kpi_id}: state {self.state!r} needs a reason")
        return self
