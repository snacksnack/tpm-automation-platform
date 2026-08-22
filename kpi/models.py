"""KPI tree models (RC1-302).

These are the rubric's "required fields per KPI" as types, so a draft that is
missing a source or a so-what cannot be represented — the shape enforces the
rubric's tests 3 and 4 and the Goodhart pairing of test 5. The judgment calls
(is this an outcome, is the mechanism real) stay in the review document; the
shape checks in `validate_shape` are the ones a human should never have to
make twice.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

KpiType = Literal["outcome", "leading"]
Direction = Literal["higher", "lower"]
Risk = Literal["low", "medium", "high"]
RejectionGround = Literal["activity", "no-decision", "unmeasurable", "duplicate", "diagnostic"]


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system: str = Field(description="Jira, eval store, billing export, simulator...")
    query: str = Field(description="The fields / query / table, precise enough to code.")
    cadence: str = Field(description="How often a fresh value can exist.")
    owner: str = Field(description="Who fixes it when it breaks.")


class Leads(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome_id: str
    mechanism: str = Field(description="Why movement here precedes movement in the outcome.")
    lead_time: str


class Goodhart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk: Risk
    gaming_path: str = Field(description="Cheapest way to move the number without the outcome.")
    counter: str = Field(description="The paired metric, or 'none' with risk low.")


class Kpi(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    name: str
    type: KpiType
    direction: Direction
    unit: str
    definition: str
    source: Source
    stale_after: str
    so_what: str = Field(description="'If this moves by X, the decision that changes is Y.'")
    goodhart: Goodhart
    failure_modes: list[str] = Field(min_length=1)
    leads: Leads | None = None
    activity_derived: bool = False


class Rejected(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    ground: RejectionGround
    reason: str
    proxy: str | None = Field(
        default=None,
        description="When ground is 'unmeasurable' and an honest proxy exists: the proxy.",
    )
    proxy_misses: str | None = Field(
        default=None, description="What the proxy misses — required whenever proxy is set."
    )


class KpiTree(BaseModel):
    model_config = ConfigDict(extra="forbid")

    program: str
    rubric_version: int
    prompt_version: int | None = None
    model: str | None = None
    sponsor_question: str = Field(
        description="The sponsor's concern in one sentence, as the tree understood it."
    )
    outcomes: list[Kpi]
    leading: list[Kpi]
    rejected: list[Rejected] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def kpis(self) -> list[Kpi]:
        return [*self.outcomes, *self.leading]


class ShapeError(ValueError):
    """The draft violates the rubric's shape rules. Listed, not raised one at a time."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def shape_problems(tree: KpiTree) -> list[str]:
    """Every rubric rule that is checkable without judgment.

    Shape (1-2 outcomes, 3-4 leading), typing consistency, every leading
    indicator naming an outcome that exists, Goodhart pairing above low risk,
    proxies carrying their caveat, and unique ids. Returns the list so a review
    can see all of them at once rather than fixing one and re-running.
    """
    problems: list[str] = []
    if not 1 <= len(tree.outcomes) <= 2:
        problems.append(f"{len(tree.outcomes)} outcome KPIs; the rubric allows 1-2")
    if not 3 <= len(tree.leading) <= 4:
        problems.append(f"{len(tree.leading)} leading indicators; the rubric allows 3-4")

    ids = [k.id for k in tree.kpis]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        problems.append(f"duplicate ids: {', '.join(dupes)}")
    outcome_ids = {k.id for k in tree.outcomes}

    for k in tree.outcomes:
        if k.type != "outcome":
            problems.append(f"{k.id} is in outcomes but typed {k.type}")
        if k.leads is not None:
            problems.append(f"{k.id} is an outcome and must not declare `leads`")
        if k.activity_derived:
            problems.append(f"{k.id} is activity-derived and cannot be an outcome (test 1)")
    for k in tree.leading:
        if k.type != "leading":
            problems.append(f"{k.id} is in leading but typed {k.type}")
        if k.leads is None:
            problems.append(f"{k.id} is a leading indicator and must say which outcome it leads")
        elif k.leads.outcome_id not in outcome_ids:
            problems.append(f"{k.id} leads {k.leads.outcome_id!r}, which is not an outcome here")
    for k in tree.kpis:
        if k.goodhart.risk != "low" and k.goodhart.counter.strip().lower() in {"", "none"}:
            problems.append(f"{k.id} has {k.goodhart.risk} Goodhart risk and no counter-metric")
    for r in tree.rejected:
        if r.proxy and not r.proxy_misses:
            problems.append(f"rejected {r.name!r} proposes a proxy without saying what it misses")
    return problems


def validate_shape(tree: KpiTree) -> KpiTree:
    problems = shape_problems(tree)
    if problems:
        raise ShapeError(problems)
    return tree


# --- rendering ---------------------------------------------------------------


def _kpi_section(k: Kpi) -> list[str]:
    leads_to = k.leads.outcome_id if k.leads else "?"
    kind = "outcome" if k.type == "outcome" else f"leading → {leads_to}"
    if k.activity_derived:
        kind += " · **activity-derived**"
    rows = [
        ("type", kind),
        ("direction", f"{k.direction} is better"),
        ("unit", k.unit),
        ("definition", k.definition),
        (
            "source",
            f"{k.source.system}: {k.source.query}. {k.source.cadence}. Owner: {k.source.owner}.",
        ),
        ("stale_after", k.stale_after),
        ("so_what", k.so_what),
    ]
    if k.leads:
        rows.append(
            (
                "leads",
                f"{k.leads.outcome_id} — mechanism: {k.leads.mechanism} "
                f"Lead time: {k.leads.lead_time}",
            )
        )
    rows.append(
        (
            "goodhart",
            f"**{k.goodhart.risk}.** Gaming path: {k.goodhart.gaming_path} "
            f"Counter: {k.goodhart.counter}",
        )
    )
    rows.append(("failure_modes", "; ".join(k.failure_modes)))
    lines = [f"### `{k.id}` · {k.name}", "", "| | |", "| --- | --- |"]
    lines += [f"| {key} | {value} |" for key, value in rows]
    return lines + [""]


def render_markdown(tree: KpiTree) -> str:
    """The reviewable document. The JSON twin is for the instrument stage."""
    head = [
        f"# KPI tree — {tree.program} — agent draft",
        "",
        f"Drafted under rubric v{tree.rubric_version}"
        + (f", define prompt v{tree.prompt_version}" if tree.prompt_version else "")
        + (f", model `{tree.model}`" if tree.model else "")
        + ". Generated; review against the hand-written baseline before adopting.",
        "",
        f"**Sponsor question, as understood:** {tree.sponsor_question}",
        "",
        "## Shape",
        "",
        "```",
    ]
    for o in tree.outcomes:
        head.append(f"{o.id:<34} {o.name}")
        for lead in tree.leading:
            if lead.leads and lead.leads.outcome_id == o.id:
                tag = "  [activity-derived]" if lead.activity_derived else ""
                head.append(f"    {lead.id:<30} {lead.name}{tag}")
    head += ["```", "", "## Outcomes", ""]
    body: list[str] = []
    for k in tree.outcomes:
        body += _kpi_section(k)
    body += ["## Leading indicators", ""]
    for k in tree.leading:
        body += _kpi_section(k)
    if tree.rejected:
        body += ["## Rejected and proxied candidates", "", "| Candidate | Ground | Reason |",
                 "| --- | --- | --- |"]
        for r in tree.rejected:
            reason = r.reason
            if r.proxy:
                reason += f" **Proxy:** {r.proxy} **Misses:** {r.proxy_misses}"
            body.append(f"| {r.name} | {r.ground} | {reason} |")
        body.append("")
    if tree.notes:
        body += ["## Notes", ""] + [f"- {n}" for n in tree.notes] + [""]
    return "\n".join(head + body)
