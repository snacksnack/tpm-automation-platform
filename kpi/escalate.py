"""Escalate: act when a KPI stops being measurable (RC1-307).

The sixth stage — the "program runs itself" half. Track (RC1-305) already
refuses to write a zero for an unmeasurable KPI: a reading is `ok`, `stale`
or `broken`, and the last two carry a reason. What track does not do is
*act*. This stage reads the day the way an owner would: when a source came
back empty or unreadable, it re-runs the collector once before believing
it; when the problem survives the retry, it names the blast radius — every
shipping KPI whose instrumented fields sit on the broken source — proposes
the fix, records the escalation beside the readings, and posts it to Slack.
The weekly brief (RC1-306) reads the same table, so an escalation is never
only a Slack message that scrolled away.

Four detections, all deterministic — no model call anywhere in this stage:

- **source**: a source in today's snapshot read `error`, or read `missing`
  after having answered before. "Never answered yet" is not an escalation
  (the simulated program's spend line is legitimately empty until week 1);
  "answered for six weeks and now returns nothing" is the week-7 silent
  break, and the health model was built to tell the two apart.
- **reading**: a KPI read `broken` for a reason no source escalation
  explains — usually a measure that raised, which is what a shape change
  looks like from here. The proposed fix is to re-run the instrument
  stage, because a changed shape invalidates the verification, not just
  the number.
- **flatline**: an `ok` value unchanged past twice its declared
  `stale_after` cadence. A stuck sensor reads exactly like a healthy
  metric, which is why nobody notices one. A KPI resting at its own ideal
  boundary (an error rate at 0, a pass rate at 100 %) is exempt — that is
  a program behaving, not a sensor stuck.
- **implausible**: an `ok` value outside the bounds its unit implies (a
  percentage past 100, negative dollars). The number is present and
  precise and cannot be true, so the measure or the source shape is
  suspect and the reading must not be trusted quietly.

The retry is the only action this stage takes on the world; everything
else is a record and a message. A retry that heals the source stores the
fresh snapshot, re-tracks the day so the store holds the healed readings,
and records the escalation as `healed` — recovery is a fact worth keeping,
not a reason to stay silent.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from collectors.models import ProgramSnapshot
from collectors.programs import Program
from config import settings
from kpi import track as track_stage
from kpi.escalations_store import Escalation, with_healed
from kpi.instrument import INSTRUMENTS, Instrumentation, load_adopted_tree
from kpi.models import Kpi, KpiTree
from kpi.reading import Reading
from store.snapshot_store import SnapshotStore

#: Sources whose instrumented fields live under a different dotted prefix:
#: both billing feeds land in the snapshot's `billing` section, so a KPI
#: citing `billing.amount_usd` is downstream of either feed.
_FIELD_PREFIX = {"anthropic-costs": "billing", "heroku-invoices": "billing"}

#: Flatline: an ok value unchanged for at least this many consecutive daily
#: readings — or twice the KPI's own stale_after, whichever is longer.
FLATLINE_FLOOR_DAYS = 7

_STALE_AFTER = re.compile(r"^\s*(\d+)\s*(?:sim-)?days?\b")

Recollect = Callable[[], ProgramSnapshot]


# --- detections ------------------------------------------------------------------------------


def blast_radius(inst: Instrumentation, source: str) -> tuple[str, ...]:
    """The shipping KPIs whose verified fields sit on `source` — what the
    instrument stage says is downstream, not what a human remembers."""
    prefix = _FIELD_PREFIX.get(source, source) + "."
    return tuple(
        k.kpi_id
        for k in inst.kpis
        if k.status == "verified" and any(f.startswith(prefix) for f in k.fields)
    )


def _previously_ok(series: list[ProgramSnapshot], source: str) -> bool:
    return any(
        h.status == "ok" for snap in series[:-1] for h in snap.health if h.source == source
    )


def _fix_for_source(program: Program, source: str, status: str, detail: str) -> str:
    if m := re.search(r"([A-Z][A-Z0-9_]+) is not set", detail):
        return (
            f"set {m.group(1)} in the environment the daily job runs with, "
            "then re-run the collector"
        )
    if source == "jira" and status == "missing":
        return (
            "the Jira query answered with no issues — the program label was likely dropped "
            f"or the filter changed; check the label behind {detail.split(':')[-1].strip()} "
            "on the board, or update the program's JQL, then re-snapshot"
        )
    if status == "missing":
        return (
            f"the {source} source answered with nothing after having answered before; "
            f"inspect it at its origin, then re-run `python -m collectors snapshot {program.id}`"
        )
    return (
        f"the {source} source could not be read ({detail.strip() or 'no detail'}); fix the "
        f"source or its credentials, then re-run `python -m collectors snapshot {program.id}`"
    )


def _last_good(series: list[ProgramSnapshot], source: str) -> ProgramSnapshot | None:
    """The most recent earlier snapshot where the source answered with rows."""
    for snap in reversed(series[:-1]):
        h = snap.source(source)
        if h is not None and h.status == "ok" and h.count > 0:
            return snap
    return None


def _source_escalations(
    program: Program, inst: Instrumentation, series: list[ProgramSnapshot], run_id: int
) -> list[Escalation]:
    from kpi.measures import SOURCE_BREAK_DROP  # one rule, defined once

    snap = series[-1]
    out: list[Escalation] = []
    for h in snap.health:
        reason = fix = None
        if h.status == "error":
            reason = f"{h.source} read error: {h.detail}"
            fix = _fix_for_source(program, h.source, h.status, h.detail)
        elif h.status == "missing" and _previously_ok(series, h.source):
            # "Never answered yet" is not an escalation; "answered before,
            # nothing today" is.
            reason = f"{h.source} read missing: {h.detail}"
            fix = _fix_for_source(program, h.source, h.status, h.detail)
        elif h.status == "ok":
            # The silent break: the source still answers, with a fraction of
            # what it used to. Same rule as the measures (SOURCE_BREAK_DROP),
            # so the escalation and the broken readings agree on the day.
            good = _last_good(series, h.source)
            ref = good.source(h.source).count if good is not None else 0
            if ref > 0 and h.count < ref * SOURCE_BREAK_DROP:
                reason = (
                    f"{h.source} answered with {h.count} row(s) against {ref} on "
                    f"{good.sim_date} — the source went quiet without erroring"
                )
                fix = (
                    "the program label was likely dropped from the issues or the filter "
                    "changed; restore the label (or fix the JQL), then re-snapshot"
                    if h.source == "jira"
                    else f"the {h.source} source lost most of its rows; inspect it at its "
                    f"origin, then re-run `python -m collectors snapshot {program.id}`"
                )
        if reason is None:
            continue
        radius = blast_radius(inst, h.source)
        if not radius:
            continue  # nothing shipping reads it; the health row is the record
        out.append(
            Escalation(
                program_id=program.id,
                sim_date=snap.sim_date,
                kind="source",
                subject=h.source,
                kpi_ids=radius,
                reason=reason,
                proposed_fix=fix,
                run_id=run_id,
            )
        )
    return out


_SHAPE_CHANGE = re.compile(r"measure raised: (TypeError|KeyError|AttributeError|ValidationError)")


def _reading_escalations(
    program: Program,
    readings: list[Reading],
    covered: set[str],
    sim_date: date,
    run_id: int,
) -> list[Escalation]:
    out: list[Escalation] = []
    for r in readings:
        if r.state != "broken" or r.kpi_id in covered:
            continue
        if "no measure is registered" in (r.reason or ""):
            fix = (
                "the instrument stage verified this KPI but nothing computes it; register "
                "a measure in `kpi/measures.py`, or re-run "
                f"`python -m kpi.instrument --program {program.id}` to re-verify the set"
            )
        elif _SHAPE_CHANGE.search(r.reason or ""):
            fix = (
                "the source shape likely changed under the measure; re-run "
                f"`python -m kpi.instrument --program {program.id}` to re-verify, "
                "and update the measure to the new shape"
            )
        else:
            fix = (
                f"inspect the day's snapshot: `python -m collectors show {program.id} "
                f"--run {run_id}` — the reason names what the measure could not use"
            )
        out.append(
            Escalation(
                program_id=program.id,
                sim_date=sim_date,
                kind="reading",
                subject=r.kpi_id,
                kpi_ids=(r.kpi_id,),
                reason=r.reason or "broken with no reason (the validator should have refused this)",
                proposed_fix=fix,
                run_id=run_id,
            )
        )
    return out


def _stale_after_days(kpi: Kpi) -> int | None:
    m = _STALE_AFTER.match(kpi.stale_after)
    return int(m.group(1)) if m else None


def _resting_at_ideal(kpi: Kpi, value: float) -> bool:
    """A value parked at its own best boundary is a program behaving, not a
    stuck sensor: an error rate at 0, a pass rate at 100 %."""
    if kpi.direction == "lower" and value == 0:
        return True
    return kpi.direction == "higher" and "%" in kpi.unit and value == 100


def _flatline_escalations(
    program: Program,
    tree: KpiTree,
    history: dict[str, list[Reading]],
    sim_date: date,
    run_id: int,
) -> list[Escalation]:
    out: list[Escalation] = []
    for kpi in [*tree.outcomes, *tree.leading]:
        series = history.get(kpi.id, [])
        window = max(FLATLINE_FLOOR_DAYS, 2 * (_stale_after_days(kpi) or 0))
        if len(series) < window:
            continue
        tail = series[-window:]
        values = {r.value for r in tail}
        if len(values) != 1 or any(r.state != "ok" for r in tail):
            continue
        (value,) = values
        if value is None or _resting_at_ideal(kpi, value):
            continue
        out.append(
            Escalation(
                program_id=program.id,
                sim_date=sim_date,
                kind="flatline",
                subject=kpi.id,
                kpi_ids=(kpi.id,),
                reason=(
                    f"value has read exactly {value:g} for {window} consecutive daily "
                    f"readings, past twice its declared cadence ({kpi.stale_after})"
                ),
                proposed_fix=(
                    "confirm the source is actually updating — a stuck sensor reads like a "
                    f"healthy metric; `python -m collectors show {program.id}` and compare "
                    "the underlying rows across days"
                ),
                run_id=run_id,
            )
        )
    return out


def _bounds(kpi: Kpi) -> tuple[float, float] | None:
    """What the unit itself rules out — impossible, never merely surprising
    (surprising is `tripped`'s job, and a false escalation here teaches
    everyone to unsubscribe). Only two shapes are bounded: a percentage of
    a finite whole (a share of cases or open points, or any percentage
    where higher is better — those cannot exceed 100), and an unsigned
    quantity (dollars per run, a burn ratio — those cannot be negative).
    A percentage *of a reference* (204 % of plan) or a signed difference
    (USD over plan) is left alone."""
    unit = kpi.unit.lower()
    if "%" in unit and ("cases" in unit or "open" in unit or kpi.direction == "higher"):
        return (0.0, 100.0)
    if unit.startswith("usd") and "over" not in unit:
        return (0.0, 1_000_000.0)
    if unit.startswith("ratio"):
        return (0.0, 1_000.0)
    return None


def _implausible_escalations(
    program: Program,
    tree: KpiTree,
    readings: list[Reading],
    run_id: int,
) -> list[Escalation]:
    by_id = {k.id: k for k in [*tree.outcomes, *tree.leading]}
    out: list[Escalation] = []
    for r in readings:
        kpi = by_id.get(r.kpi_id)
        if kpi is None or r.state != "ok" or r.value is None:
            continue
        bounds = _bounds(kpi)
        if bounds is None or bounds[0] <= r.value <= bounds[1]:
            continue
        out.append(
            Escalation(
                program_id=program.id,
                sim_date=r.sim_date,
                kind="implausible",
                subject=r.kpi_id,
                kpi_ids=(r.kpi_id,),
                reason=(
                    f"value {r.value:g} is outside what the unit ({kpi.unit}) allows "
                    f"[{bounds[0]:g}, {bounds[1]:g}] — present, precise, and cannot be true"
                ),
                proposed_fix=(
                    "do not trust the reading; the measure or the source shape is suspect — "
                    f"re-run `python -m kpi.instrument --program {program.id}` and inspect the "
                    "measure's inputs on this snapshot"
                ),
                run_id=run_id,
            )
        )
    return out


def detect(
    program: Program,
    tree: KpiTree,
    inst: Instrumentation,
    series: list[ProgramSnapshot],
    readings: list[Reading],
    run_id: int,
    history: dict[str, list[Reading]] | None = None,
) -> list[Escalation]:
    """Every escalation today's snapshot and readings justify, in one pass.

    `history` is the stored reading series per KPI (oldest first), for the
    flatline rule; without it that rule is skipped — the other three read
    the day alone.
    """
    snap = series[-1]
    escalations = _source_escalations(program, inst, series, run_id)
    covered = {kpi_id for e in escalations for kpi_id in e.kpi_ids}
    escalations += _reading_escalations(program, readings, covered, snap.sim_date, run_id)
    if history is not None:
        escalations += _flatline_escalations(program, tree, history, snap.sim_date, run_id)
    escalations += _implausible_escalations(program, tree, readings, run_id)
    return escalations


# --- the stage -------------------------------------------------------------------------------


@dataclass(frozen=True)
class EscalateResult:
    """One program, one day, read the way an owner would."""

    program_id: str
    run_id: int
    sim_date: date
    readings: list[Reading]
    escalations: list[Escalation]  # standing — survived the retry
    healed: list[Escalation]  # raised, retried, gone
    retried: bool


def _load_series(
    program: Program, *, db_path: str, sim_date: date | None
) -> tuple[list[ProgramSnapshot], int]:
    with SnapshotStore(db_path) as store:
        runs = store.program_runs(program.id)
        if not runs:
            raise ValueError(f"{program.id}: no snapshots stored; nothing to escalate")
        target = store.latest_program_run(program.id, sim_date=sim_date)
        if target is None:
            raise ValueError(f"{program.id}: no snapshot for sim-date {sim_date}")
        series = [
            store.load_program_snapshot(r.run_id) for r in runs if r.run_id <= target.run_id
        ]
    return series, target.run_id


def escalate_program(
    program: Program,
    *,
    db_path: str,
    sim_date: date | None = None,
    instruments_dir: Path = INSTRUMENTS,
    history: dict[str, list[Reading]] | None = None,
    recollect: Recollect | None = None,
) -> EscalateResult:
    """Detect, retry once, and settle what stands.

    `recollect` re-runs the program's collector; when the fresh snapshot
    heals every failing source, it is stored, the day is re-tracked against
    it, and the escalations it explains are recorded as healed. The caller
    stores the result — this function touches the snapshot store only to
    read, and to save the one healed snapshot.
    """
    tree = load_adopted_tree(program.id)
    inst = track_stage.load_instrumentation(program.id, instruments_dir=instruments_dir)
    series, run_id = _load_series(program, db_path=db_path, sim_date=sim_date)
    readings = track_stage.track(program, series, inst.computes)
    escalations = detect(program, tree, inst, series, readings, run_id, history)

    healed: list[Escalation] = []
    retried = False
    if any(e.kind == "source" for e in escalations) and recollect is not None:
        retried = True
        fresh = recollect()
        candidate = [*series, fresh]
        fresh_readings = track_stage.track(program, candidate, inst.computes)
        still = detect(program, tree, inst, candidate, fresh_readings, run_id, history)
        still_subjects = {(e.kind, e.subject) for e in still}
        cleared = [e for e in escalations if (e.kind, e.subject) not in still_subjects]
        if cleared:
            # The retry actually changed the day: keep the snapshot, and let
            # the caller re-store the healed readings under its run id.
            with SnapshotStore(db_path) as store:
                run_id = store.save_program_snapshot(fresh, project_key=program.project_key)
            series = candidate
            readings = fresh_readings
            escalations = detect(program, tree, inst, series, readings, run_id, history)
            healed = [
                with_healed(
                    e,
                    fix_applied=(
                        "healed on retry: the collector re-ran and the source answered; "
                        f"the day was re-tracked against run {run_id}"
                    ),
                )
                for e in cleared
            ]

    return EscalateResult(
        program_id=program.id,
        run_id=run_id,
        sim_date=series[-1].sim_date,
        readings=readings,
        escalations=escalations,
        healed=healed,
        retried=retried,
    )


# --- rendering -------------------------------------------------------------------------------


def render_alert(result: EscalateResult, program_name: str) -> str:
    """The Slack message: what broke, what it takes down, what to do."""
    out = [f"*KPI escalation — {program_name} — {result.sim_date}*"]
    for e in result.escalations:
        radius = ", ".join(f"`{k}`" for k in e.kpi_ids)
        out += [
            f"🔴 *{e.kind}: {e.subject}* — {e.reason}",
            f"    blast radius: {radius}",
            f"    proposed fix: {e.proposed_fix}",
        ]
    for e in result.healed:
        out.append(f"🟢 *healed: {e.subject}* — {e.reason} — {e.proposed_fix}")
    out.append(
        f"_Escalate stage, snapshot run {result.run_id}; the affected readings are labelled "
        "in `kpi_readings`, never zeroed._"
    )
    return "\n".join(out)


def _print_result(result: EscalateResult) -> None:
    print(f"{result.program_id}: {result.sim_date} (run {result.run_id})")
    if not result.escalations and not result.healed:
        print("  nothing to escalate")
    for e in result.escalations:
        print(f"  ! {e.kind:<11} {e.subject:<22} {e.reason}")
        print(f"    -> {e.proposed_fix}")
        print(f"    blast radius: {', '.join(e.kpi_ids)}")
    for e in result.healed:
        print(f"    {e.kind:<11} {e.subject:<22} healed on retry")
    if result.retried:
        print("  (collector was re-run once)")


# --- CLI -------------------------------------------------------------------------------------


def _default_recollect(program: Program) -> Recollect:
    """The same wiring `python -m collectors snapshot` uses."""

    def recollect() -> ProgramSnapshot:
        from collectors import program as collect
        from collectors.jira import JiraCollector

        jira = None
        if program.jira and settings.jira_email and settings.jira_api_token:
            jira = JiraCollector(
                settings.jira_base_url, settings.jira_email, settings.jira_api_token
            )
        try:
            return collect.collect_program(
                program,
                jira=jira,
                eval_dsn=os.environ.get("EVAL_DATABASE_URL"),
                heroku_api_key=settings.heroku_api_key,
                anthropic_admin_key=settings.anthropic_admin_key,
            )
        finally:
            if jira is not None:
                jira.__exit__(None, None, None)

    return recollect


def main(argv: list[str] | None = None) -> int:
    """Exit codes mirror the other stages: 0 nothing stands (healed counts as
    nothing standing); 1 at least one escalation stands; 2 the stage could
    not run. The escalations are recorded either way — a day something broke
    is exactly the day worth recording."""
    from collectors import programs

    ap = argparse.ArgumentParser(
        prog="python -m kpi.escalate",
        description="Detect unmeasurable KPIs, retry the collector, escalate what stands.",
    )
    ap.add_argument("--program", required=True, choices=sorted(programs.PROGRAMS))
    ap.add_argument("--db", default=settings.db_path, help="snapshot store (default: DB_PATH)")
    ap.add_argument(
        "--sim-date", type=date.fromisoformat, default=None,
        help="escalate this day rather than the newest (YYYY-MM-DD)",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="detect and print only — no retry, no Postgres, no Slack",
    )
    ap.add_argument(
        "--no-post", action="store_true",
        help="record to Postgres but do not post to Slack",
    )
    args = ap.parse_args(argv)

    program = programs.get(args.program)
    dsn = os.environ.get("EVAL_DATABASE_URL")

    history: dict[str, list[Reading]] | None = None
    if not args.dry_run:
        if not dsn:
            print(
                "EVAL_DATABASE_URL is not set (it lives in ~/.zshrc — RC1-263); "
                "run with --dry-run to detect without recording",
                file=sys.stderr,
            )
            return 2
        from kpi.readings_store import ReadingsStore

        with ReadingsStore(dsn) as readings_store:
            history = {}
            for sr in readings_store.readings(program.id):
                history.setdefault(sr.reading.kpi_id, []).append(sr.reading)

    try:
        result = escalate_program(
            program,
            db_path=args.db,
            sim_date=args.sim_date,
            history=history,
            recollect=None if args.dry_run else _default_recollect(program),
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"{args.program}: {exc}", file=sys.stderr)
        return 2

    if not args.dry_run:
        from drift.notify import SlackWebhookSender
        from kpi.escalations_store import EscalationsStore

        with EscalationsStore(dsn) as store:
            store.save([*result.escalations, *result.healed])
            if result.healed:
                from kpi.readings_store import ReadingsStore

                with ReadingsStore(dsn) as readings_store:
                    readings_store.save(
                        result.program_id, result.readings, run_id=result.run_id
                    )
            if result.escalations and not args.no_post:
                webhook = settings.slack_webhook_url
                fresh = [e for e in result.escalations if not store.already_raised(e)]
                if not webhook:
                    print(
                        "SLACK_WEBHOOK_URL is not set; escalations are recorded but not posted",
                        file=sys.stderr,
                    )
                elif fresh:
                    posted = EscalateResult(
                        program_id=result.program_id,
                        run_id=result.run_id,
                        sim_date=result.sim_date,
                        readings=result.readings,
                        escalations=fresh,
                        healed=result.healed,
                        retried=result.retried,
                    )
                    SlackWebhookSender(webhook).channel(render_alert(posted, program.name))
                    store.mark_posted(fresh)
                    print(f"posted {len(fresh)} escalation(s) to Slack", file=sys.stderr)
                else:
                    print(
                        "all standing escalations were already posted this week; not re-posting",
                        file=sys.stderr,
                    )

    _print_result(result)
    return 1 if result.escalations else 0


if __name__ == "__main__":
    raise SystemExit(main())
