"""The source catalog: what a program's snapshots actually hold (RC1-303).

The rubric's test 4 says a KPI names a source; the instrument stage verifies
it. Verification needs a thing to verify against, and that thing is not the
brief's data-source table — it is what the collector (RC1-301) stores. The
catalog is that, as data: every field of every source a program has, the
constants it declares, the sources it does *not* have, and the latest stored
snapshot's health. The model reads it to propose verdicts; the code checks
every field a verdict cites against it.

Two honesty features. `not_available` lists what a reader might assume is
there and is not — per-characteristic detail and token counts for eval runs
(the snapshot keeps counts), the Jira changelog (never read), GitHub (not a
source at all). And `constants` are listed separately from measured fields,
so a KPI that leans on one is a proxy with a caveat, never a measurement.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from collectors.models import ProgramSnapshot
from collectors.programs import Program


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    description: str


@dataclass(frozen=True)
class Source:
    name: str
    cadence: str
    fields: tuple[Field, ...]
    notes: str = ""


_JIRA = Source(
    "jira",
    "daily snapshot; the value is current state on that day",
    (
        Field("key", "str", "issue key"),
        Field("issue_type", "str", "Story, Epic…; the epic carries the committed date"),
        Field("summary", "str", ""),
        Field("status", "str", "status name as Jira shows it (To Do, In Progress, Blocked, Done…)"),
        Field("status_category", "str", "To Do | In Progress | Done"),
        Field("assignee_name", "str | None", ""),
        Field("due", "date | None", "duedate"),
        Field("start", "date | None", "start date (customfield_10015)"),
        Field("points", "float | None", "story points (customfield_10033)"),
        Field(
            "labels", "list[str]",
            "program label, ks-<slug>, ws-<workstream>, own-<owner>, ga-blocking",
        ),
        Field("created", "date | None", "issue creation date"),
        Field("parent", "str | None", "the epic key"),
        Field("links", "list[(upstream, downstream)]", "Blocks links, upstream blocks downstream"),
    ),
    "Only issues carrying the program label are in the snapshot; the changelog is never read.",
)
_SPEND = Source(
    "spend",
    "weekly; a week's row lands the Monday after",
    (
        Field("week", "int", "1-based program week"),
        Field("week_start", "date", ""),
        Field("planned_usd", "float", ""),
        Field("actual_usd", "float", ""),
        Field("landed_on_day", "int | None", "sim-day the row became available"),
    ),
    "The simulator's line for the simulated program; a billing export for a real one (RC1-308).",
)
_EVAL = Source(
    "eval-store",
    "one row per run, whenever a suite is run; the snapshot is taken daily",
    (
        Field("run_id", "str", ""),
        Field("subject", "str", ""),
        Field("code_version", "str", "the consumer package version under test"),
        Field("model", "str | None", "None for a deterministic subject"),
        Field("started_at", "datetime", ""),
        Field("cases", "int", "cases in the run"),
        Field("passed", "int", "cases that passed every gating characteristic (case-weighted)"),
        Field("errored", "int", "cases that produced nothing to score"),
        Field("cost_usd", "float", "sum of the run's case costs"),
    ),
    "Counts, not records: per-case characteristics, advisory flags, token counts and "
    "prompt_version are not carried.",
)
_CLOCK = Source(
    "clock",
    "daily",
    (
        Field("sim_date", "date", "the day the snapshot is for"),
        Field("sim_day", "int | None", "0-based program day (simulated programs only)"),
        Field("collected_at", "datetime", "wall clock"),
    ),
)

_NOT_AVAILABLE = {
    "jira": [
        "Jira changelog / transition history (KPIs are snapshot diffs, never changelog)",
        "comments, worklogs, sprint fields",
    ],
    "spend": ["per-service or per-resource breakdown", "forecast or budget revisions"],
    "eval-store": [
        "per-case characteristics and whether each is advisory (the snapshot keeps counts)",
        "token counts and per-case latency",
        "prompt_version",
        "the model price table",
        "branch or pull request a run was taken on",
    ],
    "other": [
        "GitHub repositories: commits, tags, releases, pull requests",
        "Postgres plan billing as a feed (a plan price may be declared as a constant)",
        "Heroku / Vercel billing exports",
    ],
}


def sources_for(program: Program) -> list[Source]:
    out = [_CLOCK]
    if program.jira:
        out.append(_JIRA)
    if program.spend_csv:
        out.append(_SPEND)
    if program.eval_store:
        out.append(_EVAL)
    return out


def available_fields(program: Program) -> set[str]:
    """Dotted names a verdict may cite: `jira.status`, `spend.actual_usd`,
    `eval-store.passed`, `constants.store_plan_usd_per_month`…"""
    names = {f"{s.name}.{f.name}" for s in sources_for(program) for f in s.fields}
    names |= {f"constants.{k}" for k in program.constants}
    return names


def catalog(program: Program, sample: ProgramSnapshot | None = None) -> dict:
    """The catalog as the model sees it and the checks read it."""
    not_available = {k: v for k, v in _NOT_AVAILABLE.items() if k == "other"}
    for s in sources_for(program):
        if s.name in _NOT_AVAILABLE:
            not_available[s.name] = _NOT_AVAILABLE[s.name]
    out: dict = {
        "program": program.id,
        # JSON-native (lists, not tuples): what the model receives is what the
        # checks read, byte for byte.
        "sources": [
            {**asdict(s), "fields": [asdict(f) for f in s.fields]} for s in sources_for(program)
        ],
        "constants": dict(program.constants),
        "not_available": not_available,
        "field_names": sorted(available_fields(program)),
    }
    if sample is not None:
        out["sample"] = {
            "sim_date": sample.sim_date.isoformat(),
            "sim_day": sample.sim_day,
            "collected_at": sample.collected_at.isoformat(),
            "health": [h.model_dump() for h in sample.health],
            "counts": {
                "jira_issues": None if sample.jira is None else len(sample.jira.issues),
                "jira_links": None if sample.jira is None else len(sample.jira.links),
                "spend_rows": len(sample.spend),
                "eval_runs": len(sample.eval_runs),
            },
        }
    return out
