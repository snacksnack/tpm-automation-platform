"""Measures: a computation per confirmed KPI over a snapshot series (RC1-303).

The instrument stage's "confirmed" is only worth something if a computation
exists and runs — so this is the registry the stage checks against, and the
registry the track stage (RC1-305) will schedule. A measure takes the
program and its stored snapshots, oldest first, and returns a `Reading` for
the latest one. Numbers are computed here, by code; nothing in this module
calls a model.

Two families, both implemented here. The eval-run-store KPIs read the
snapshot's run rows. The simulated program's six read the snapshot's Jira
project and spend line — written against the adopted tree's definitions
(`docs/kpi/trees/simulated-program.review.md`), and deliberately *not*
against `simulate/ledger.py`: the ledger is the independently-written
expectation the `kpi-ledger` eval diffs these formulas against, and nothing
under `kpi/` may import `simulate` (RC1-310 — the shipped package must not
lean on the development-only one).

`source_missing` is the instrument stage's planted-break check: the latest
snapshot with every source gone. Every confirmed measure is run against it
and must read `broken` or `stale` with a reason — never a number. That is
the rubric's staleness rule, verified per KPI rather than promised.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from collectors.models import EvalRunRow, ProgramSnapshot, SourceHealth, SpendRow
from collectors.programs import Program
from kpi.reading import Reading

Measure = Callable[[Program, list[ProgramSnapshot]], Reading]

WINDOW_DAYS = 28
FRESH_DAYS = 7
PASS_RATE_FLOOR = 80.0
ERROR_RATE_CEILING = 20.0
FRESHNESS_CEILING_DAYS = 7
COST_RISE_TRIP = 1.5
MODEL_COST_RATIO_TRIP = 3.0


# --- helpers ----------------------------------------------------------------------------------


def _latest_per_subject(rows: list[EvalRunRow]) -> dict[str, EvalRunRow]:
    latest: dict[str, EvalRunRow] = {}
    for r in sorted(rows, key=lambda r: r.started_at):
        latest[r.subject] = r
    return latest


def _billed(rows: list[EvalRunRow]) -> list[EvalRunRow]:
    return [r for r in rows if r.model]


def _in_window(rows: list[EvalRunRow], today: date, days: int = WINDOW_DAYS) -> list[EvalRunRow]:
    floor = today - timedelta(days=days)
    return [r for r in rows if r.started_at.date() > floor]


def _eval_source_gone(snap: ProgramSnapshot) -> str | None:
    h = snap.source("eval-store")
    if h is None or h.status == "error":
        return f"eval store unreadable: {h.detail if h else 'no eval-store source'}"
    if h.status == "missing" or not snap.eval_runs:
        return "eval store answered with no runs"
    return None


def _broken(kpi_id: str, snap: ProgramSnapshot, reason: str, **kw) -> Reading:
    return Reading(
        kpi_id=kpi_id, sim_date=snap.sim_date, value=None, state="broken", reason=reason, **kw
    )


# --- eval-run-store measures ----------------------------------------------------------------


def gated_pass_rate(program: Program, series: list[ProgramSnapshot]) -> Reading:
    snap = series[-1]
    if gone := _eval_source_gone(snap):
        return _broken("gated-pass-rate", snap, gone)
    latest = _latest_per_subject(_billed(snap.eval_runs))
    rates: dict[str, float] = {}
    no_signal = []
    for subject, r in latest.items():
        scorable = r.cases - r.errored
        if scorable <= 0:
            no_signal.append(subject)
        else:
            rates[subject] = r.passed / scorable * 100
    if not rates:
        return _broken("gated-pass-rate", snap, "no billed subject has a scorable case (no-signal)")
    worst, value = min(rates.items(), key=lambda kv: kv[1])
    age = (snap.sim_date - latest[worst].started_at.date()).days
    state, reason = _freshness(age, f"{worst}'s latest run is {age} days old")
    previous = _previous_value(program, series, gated_pass_rate)
    tripped = value < PASS_RATE_FLOOR and previous is not None and previous < PASS_RATE_FLOOR
    detail = "; ".join(f"{s} {v:.0f} %" for s, v in sorted(rates.items(), key=lambda kv: kv[1]))
    if no_signal:
        detail += f"; no-signal: {', '.join(no_signal)}"
    return Reading(
        kpi_id="gated-pass-rate", sim_date=snap.sim_date, value=round(value, 1), state=state,
        reason=reason, tripped=tripped, as_of=latest[worst].started_at.date(),
        detail=f"minimum is {worst}; {detail}",
    )


def cost_per_verified_case(program: Program, series: list[ProgramSnapshot]) -> Reading:
    snap = series[-1]
    if gone := _eval_source_gone(snap):
        return _broken("cost-per-verified-case", snap, gone)
    window = _in_window(snap.eval_runs, snap.sim_date)
    scored = sum(r.cases - r.errored for r in window)
    spend = sum(r.cost_usd for r in window)
    plan = program.constants.get("store_plan_usd_per_month")
    if plan is None:
        return _broken(
            "cost-per-verified-case", snap, "no store_plan_usd_per_month constant declared"
        )
    fixed = plan * WINDOW_DAYS / 30
    if scored == 0:
        return _broken(
            "cost-per-verified-case", snap,
            f"no scored cases in the trailing {WINDOW_DAYS} days; only the ${fixed:.2f} fixed cost",
        )
    value = (spend + fixed) / scored
    newest = max(r.started_at.date() for r in window)
    age = (snap.sim_date - newest).days
    state, reason = _freshness(age, f"latest run is {age} days old")
    sweep = sum(r.cost_usd for r in _latest_per_subject(_billed(snap.eval_runs)).values())
    previous = _previous_value(program, series, cost_per_verified_case)
    tripped = previous is not None and previous > 0 and value > previous * COST_RISE_TRIP
    free_billed = [r.subject for r in _billed(window) if r.cost_usd == 0]
    detail = (
        f"${spend:.2f} model + ${fixed:.2f} store over {scored} scored cases "
        f"({len(window)} runs); $ per full sweep {sweep:.2f}"
    )
    if free_billed:
        detail += f"; BROKEN INSTRUMENT: $0 billed run(s) for {', '.join(sorted(set(free_billed)))}"
    return Reading(
        kpi_id="cost-per-verified-case", sim_date=snap.sim_date, value=round(value, 4),
        state=state, reason=reason, tripped=tripped, as_of=newest, detail=detail,
    )


def measurement_freshness_days(program: Program, series: list[ProgramSnapshot]) -> Reading:
    snap = series[-1]
    if gone := _eval_source_gone(snap):
        return _broken("measurement-freshness-days", snap, gone)
    latest = _latest_per_subject(_billed(snap.eval_runs))
    if not latest:
        return _broken("measurement-freshness-days", snap, "no billed subject has ever run")
    ages = {s: (snap.sim_date - r.started_at.date()).days for s, r in latest.items()}
    worst, value = max(ages.items(), key=lambda kv: kv[1])
    over = sorted(s for s, a in ages.items() if a > FRESHNESS_CEILING_DAYS)
    return Reading(
        kpi_id="measurement-freshness-days", sim_date=snap.sim_date, value=float(value),
        tripped=value > FRESHNESS_CEILING_DAYS, as_of=snap.sim_date,
        detail=f"oldest is {worst} at {value} days; {len(over)} of {len(ages)} billed subjects "
        f"over {FRESHNESS_CEILING_DAYS} days" + (f": {', '.join(over)}" if over else ""),
    )


def error_rate(program: Program, series: list[ProgramSnapshot]) -> Reading:
    snap = series[-1]
    if gone := _eval_source_gone(snap):
        return _broken("error-rate", snap, gone)
    latest = _latest_per_subject(snap.eval_runs)
    rates = {s: r.errored / r.cases * 100 for s, r in latest.items() if r.cases}
    if not rates:
        return _broken("error-rate", snap, "no run has any cases (no-signal)")
    worst, value = max(rates.items(), key=lambda kv: kv[1])
    r = latest[worst]
    return Reading(
        kpi_id="error-rate", sim_date=snap.sim_date, value=round(value, 1),
        tripped=value > ERROR_RATE_CEILING, as_of=r.started_at.date(),
        detail=f"worst is {worst}: {r.errored} of {r.cases} cases errored; "
        f"{sum(1 for v in rates.values() if v > ERROR_RATE_CEILING)} subject(s) over "
        f"{ERROR_RATE_CEILING:.0f} %",
    )


def cost_per_run_by_model(program: Program, series: list[ProgramSnapshot]) -> Reading:
    snap = series[-1]
    if gone := _eval_source_gone(snap):
        return _broken("cost-per-run-by-model", snap, gone)
    window = _billed(_in_window(snap.eval_runs, snap.sim_date))
    if not window:
        return _broken(
            "cost-per-run-by-model", snap, f"no billed run in the trailing {WINDOW_DAYS} days"
        )
    groups: dict[tuple[str, str], list[EvalRunRow]] = {}
    for r in window:
        groups.setdefault((r.subject, r.model or ""), []).append(r)
    means = {k: sum(r.cost_usd for r in v) / len(v) for k, v in groups.items()}
    (subject, model), value = max(means.items(), key=lambda kv: kv[1])
    tripped_pairs = []
    for (s, m), cost in means.items():
        cheapest = min(c for (s2, _), c in means.items() if s2 == s)
        if cheapest > 0 and cost > cheapest * MODEL_COST_RATIO_TRIP:
            tripped_pairs.append(f"{s} on {m} ({cost / cheapest:.1f}x)")
    newest = max(r.started_at.date() for r in window)
    age = (snap.sim_date - newest).days
    state, reason = _freshness(age, f"latest billed run is {age} days old")
    detail = (
        f"dearest is {subject} on {model} at ${value:.3f}/run; {len(means)} (subject, model) "
        f"pairs over {len(window)} runs"
    )
    if tripped_pairs:
        detail += f"; over 3x: {', '.join(tripped_pairs)}"
    return Reading(
        kpi_id="cost-per-run-by-model", sim_date=snap.sim_date, value=round(value, 4),
        state=state, reason=reason, tripped=bool(tripped_pairs), as_of=newest, detail=detail,
    )


# Real billing (RC1-308): the org cost report lands daily, an invoice monthly.
METERED_STALE_DAYS = 3
INVOICE_STALE_DAYS = 40
REAL_COST_RATIO_TRIP = 3.0  # real spend vs price-table attribution


def _billing_components(snap: ProgramSnapshot) -> tuple[float, float, float, int] | None:
    """(model_spend, store_cost, attributed, runs) over the trailing window,
    or None when a feed has no rows or no run was taken. Shared with the
    two-consecutive trip so both readings use one definition."""
    metered = [b for b in snap.billing if b.source == "anthropic-costs"]
    invoices = [b for b in snap.billing if b.source == "heroku-invoices"]
    if not metered or not invoices:
        return None
    floor = snap.sim_date - timedelta(days=WINDOW_DAYS)
    model_spend = sum(b.amount_usd for b in metered if b.period_start > floor)
    invoice = max(invoices, key=lambda b: b.period_end)
    period_days = max((invoice.period_end - invoice.period_start).days, 1)
    store_cost = invoice.amount_usd / period_days * WINDOW_DAYS
    runs = _in_window(snap.eval_runs, snap.sim_date)
    if not runs:
        return None
    attributed = sum(r.cost_usd for r in runs)
    return model_spend, store_cost, attributed, len(runs)


def real_cost_per_run(program: Program, series: list[ProgramSnapshot]) -> Reading:
    """What a run of the measurement program really costs: billed dollars —
    the org's metered model spend plus the store's actual invoice, prorated —
    over the runs taken in the trailing window. The price-table attribution
    rides in the detail as the Goodhart counter: a widening gap is spend the
    program cannot account for, and that gap is the trip."""
    snap = series[-1]
    for feed in ("anthropic-costs", "heroku-invoices"):
        h = snap.source(feed)
        if h is None or h.status == "error":
            return _broken(
                "real-cost-per-run", snap,
                f"{feed} unreadable: {h.detail if h else 'no billing source configured'}",
            )
    parts = _billing_components(snap)
    if parts is None:
        if not any(b.source == "anthropic-costs" for b in snap.billing):
            return _broken("real-cost-per-run", snap, "anthropic-costs answered with no periods")
        if not any(b.source == "heroku-invoices" for b in snap.billing):
            return _broken("real-cost-per-run", snap, "heroku-invoices answered with no periods")
        spent = sum(
            b.amount_usd
            for b in snap.billing
            if b.source == "anthropic-costs"
            and b.period_start > snap.sim_date - timedelta(days=WINDOW_DAYS)
        )
        return _broken(
            "real-cost-per-run", snap,
            f"no run in the trailing {WINDOW_DAYS} days: ${spent:.2f} of real model spend "
            "with nothing measured",
        )
    model_spend, store_cost, attributed, runs = parts
    real = model_spend + store_cost
    value = round(real / runs, 4)

    latest_metered = max(
        b.period_end for b in snap.billing if b.source == "anthropic-costs"
    )  # exclusive end: yesterday's bucket ends today, so a live feed reads age 0
    invoice_end = max(b.period_end for b in snap.billing if b.source == "heroku-invoices")
    state, reason = "ok", None
    if (age := (snap.sim_date - latest_metered).days) > METERED_STALE_DAYS:
        state, reason = "stale", f"latest cost-report bucket is {age} days old"
    elif (age := (snap.sim_date - invoice_end).days) > INVOICE_STALE_DAYS:
        state, reason = "stale", f"latest store invoice closed {age} days ago"

    ratio = real / attributed if attributed > 0 else None
    previous = _billing_components(series[-2]) if len(series) >= 2 else None
    prev_ratio = (
        (previous[0] + previous[1]) / previous[2]
        if previous is not None and previous[2] > 0
        else None
    )
    tripped = (
        ratio is not None and ratio > REAL_COST_RATIO_TRIP
        and prev_ratio is not None and prev_ratio > REAL_COST_RATIO_TRIP
    )
    # Scoped rows ship beside an org-total twin (RC1-327); their presence is
    # what says the spend is the eval workspace's exactly, not an upper bound.
    org_rows = [b for b in snap.billing if b.source == "anthropic-costs-org"]
    floor = snap.sim_date - timedelta(days=WINDOW_DAYS)
    if org_rows:
        org_spend = sum(b.amount_usd for b in org_rows if b.period_start > floor)
        detail = (
            f"${model_spend:.2f} eval-workspace model spend + ${store_cost:.2f} store "
            f"over {runs} run(s); price-table attribution ${attributed:.2f}"
            + (f" ({ratio:.1f}x)" if ratio is not None else "; nothing attributed")
            + f" — org-wide ${org_spend:.2f} for the fleet picture"
        )
    else:
        detail = (
            f"${model_spend:.2f} org model spend + ${store_cost:.2f} store over {runs} run(s); "
            f"price-table attribution ${attributed:.2f}"
            + (f" ({ratio:.1f}x)" if ratio is not None else "; nothing attributed")
            + " — org-wide feed, so attribution is an upper bound"
        )
    return Reading(
        kpi_id="real-cost-per-run", sim_date=snap.sim_date, value=value, state=state,
        reason=reason, tripped=tripped, as_of=latest_metered, detail=detail,
    )


def _freshness(age: int, reason: str) -> tuple[str, str | None]:
    return ("stale", reason) if age > FRESH_DAYS else ("ok", None)


def _previous_value(
    program: Program, series: list[ProgramSnapshot], measure: Measure
) -> float | None:
    """The same measure one snapshot earlier, for "two consecutive" rules."""
    if len(series) < 2:
        return None
    return measure(program, series[:-1]).value


# --- the simulated program: the adopted tree's formulas over collected snapshots -------------

SIM_WINDOW_DAYS = 14  # trailing throughput window (the tree review kept 14 over 28)
SIM_STATUS_DONE = "Done"  # the tree keys on status names, not categories
SIM_STATUS_BLOCKED = "Blocked"
SIM_SLUG_PREFIX = "ks-"  # the simulator's per-story slug label; falls back to the Jira key
GA_BLOCKING_LABEL = "ga-blocking"  # the sign-off root; chains into it are the critical path
SOURCE_BREAK_DROP = 0.5  # story count under this share of the last good day's = broken
SPEND_STALE_AFTER_DAYS = 8

FORECAST_TRIP_DAYS = 5.0
SCOPE_TRIP_PCT = 10.0
SLACK_TRIP_DAYS = 3.0
BLOCKED_TRIP_PCT = 25.0
BLOCKED_TRIP_DAYS = 3
COST_TRIP_RATIO = 1.10
BURN_TRIP_SINGLE = 1.5
BURN_TRIP_CONSECUTIVE = 1.2


@dataclass(frozen=True)
class _Story:
    slug: str
    blocked: bool
    done: bool
    start: date
    due: date
    points: int
    ga_blocking: bool
    blocks: tuple[str, ...]  # slugs this story blocks


@dataclass(frozen=True)
class _Frame:
    """One sim-day of the simulated program, read out of its collected snapshot."""

    day: int
    date: date
    ga_date: date | None  # the epic's due date: the committed GA
    stories: dict[str, _Story]

    @property
    def open(self) -> dict[str, _Story]:
        return {slug: s for slug, s in self.stories.items() if not s.done}

    @property
    def total_points(self) -> int:
        return sum(s.points for s in self.stories.values())


def _frame(snap: ProgramSnapshot) -> _Frame:
    stories: dict[str, _Story] = {}
    ga_date: date | None = None
    project = snap.jira
    if project is not None:
        slug_of = {
            i.key: next(
                (lb[len(SIM_SLUG_PREFIX):] for lb in i.labels if lb.startswith(SIM_SLUG_PREFIX)),
                i.key,
            )
            for i in project.issues
        }
        blocks: dict[str, list[str]] = {}
        for link in project.links:
            if link.upstream in slug_of and link.downstream in slug_of:
                blocks.setdefault(slug_of[link.upstream], []).append(slug_of[link.downstream])
        for i in project.issues:
            if i.issue_type == "Epic":
                ga_date = i.due
                continue
            if i.start is None or i.due is None:
                continue  # a story with no dates cannot sit on a schedule; reported by RC1-303
            slug = slug_of[i.key]
            stories[slug] = _Story(
                slug=slug,
                blocked=i.status == SIM_STATUS_BLOCKED,
                done=i.status == SIM_STATUS_DONE,
                start=i.start,
                due=i.due,
                points=int(i.points or 0),
                ga_blocking=GA_BLOCKING_LABEL in i.labels,
                blocks=tuple(sorted(blocks.get(slug, ()))),
            )
    return _Frame(day=snap.sim_day, date=snap.sim_date, ga_date=ga_date, stories=stories)


def _upstreams(stories: dict[str, _Story]) -> dict[str, set[str]]:
    """slug -> slugs that block it, within the frame."""
    ups: dict[str, set[str]] = {slug: set() for slug in stories}
    for slug, story in stories.items():
        for down in story.blocks:
            if down in ups:
                ups[down].add(slug)
    return ups


def _ancestors(slug: str, ups: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    stack = list(ups.get(slug, ()))
    while stack:
        s = stack.pop()
        if s in seen:
            continue
        seen.add(s)
        stack.extend(ups.get(s, ()))
    return seen


def _ga_chain(stories: dict[str, _Story]) -> set[str]:
    """Every story on a chain to a GA-blocking story, roots included."""
    ups = _upstreams(stories)
    roots = {slug for slug, s in stories.items() if s.ga_blocking}
    chain = set(roots)
    for root in roots:
        chain |= _ancestors(root, ups)
    return chain


def _last_good(frames: list[_Frame]) -> int:
    """The most recent day the source-break rule trusts: the story count under
    the program never falls below half of the last trusted day's."""
    good = 0
    for day in range(1, len(frames)):
        reference = len(frames[good].stories)
        if not (reference > 0 and len(frames[day].stories) < reference * SOURCE_BREAK_DROP):
            good = day
    return good


def _sim_forecast_slip(frames: list[_Frame]) -> Reading:
    snap = frames[-1]
    if snap.ga_date is None:
        return Reading(
            kpi_id="forecast-slip-days", sim_date=snap.date, value=None, state="broken",
            reason="the snapshot names no committed GA date (no epic with a due date)",
        )
    remaining = sum(s.points for s in snap.open.values())
    if remaining == 0:
        # The first day of the *current* all-Done stretch; an empty snapshot
        # does not count as one (that is the source break, handled upstream).
        completed = snap.day
        while completed > 0 and frames[completed - 1].stories and not frames[completed - 1].open:
            completed -= 1
        value = float((frames[completed].date - snap.ga_date).days)
        return Reading(
            kpi_id="forecast-slip-days", sim_date=snap.date, value=value, as_of=snap.date,
            tripped=value > FORECAST_TRIP_DAYS,
            detail=f"all work Done on day {completed}; committed GA {snap.ga_date}",
        )
    if snap.day >= SIM_WINDOW_DAYS:
        then = frames[snap.day - SIM_WINDOW_DAYS]
        done = sum(
            s.points
            for slug, s in snap.stories.items()
            if s.done and not (slug in then.stories and then.stories[slug].done)
        )
        rate = done / SIM_WINDOW_DAYS
        basis = f"{done} pts Done in the last {SIM_WINDOW_DAYS} days"
    else:
        # No throughput history yet: assume the plan's own rate — every point
        # today over the program's whole weeks, kickoff to the GA week's end.
        plan_days = 7 * ((snap.ga_date - frames[0].date).days // 7 + 1)
        rate = snap.total_points / plan_days
        basis = (
            f"plan rate {snap.total_points} pts / {plan_days} days "
            f"(under {SIM_WINDOW_DAYS} days of history)"
        )
    if rate == 0:
        return Reading(
            kpi_id="forecast-slip-days", sim_date=snap.date, value=None, state="broken",
            reason="no completed work to forecast from", as_of=snap.date,
            detail=f"{remaining} pts remaining; {basis}",
        )
    value = round((snap.date - snap.ga_date).days + remaining / rate, 2)
    return Reading(
        kpi_id="forecast-slip-days", sim_date=snap.date, value=value, as_of=snap.date,
        tripped=value > FORECAST_TRIP_DAYS,
        detail=f"{remaining} pts remaining at {rate:.3f} pts/day ({basis})",
    )


def _sim_scope_change(frames: list[_Frame]) -> Reading:
    snap, base = frames[-1], frames[0]
    baseline = base.total_points
    if baseline == 0:
        return Reading(
            kpi_id="scope-change-pct", sim_date=snap.date, value=None, state="broken",
            reason="the day-0 snapshot holds no stories to baseline against",
        )
    added = sum(s.points for slug, s in snap.stories.items() if slug not in base.stories)
    removed = sum(s.points for slug, s in base.stories.items() if slug not in snap.stories)
    value = round((added - removed) / baseline * 100, 2)
    return Reading(
        kpi_id="scope-change-pct", sim_date=snap.date, value=value, as_of=snap.date,
        tripped=value > SCOPE_TRIP_PCT,
        detail=f"+{added} / -{removed} pts against a {baseline}-pt baseline",
    )


def _sim_critical_path_slack(frames: list[_Frame]) -> Reading:
    snap = frames[-1]
    chain = _ga_chain(snap.stories)
    links: list[tuple[int, str, str]] = []
    for up_slug, up in snap.stories.items():
        if up.done:
            continue  # a delivered upstream cannot consume slack
        for down_slug in up.blocks:
            down = snap.stories.get(down_slug)
            if down is None or down_slug not in chain or down.done:
                continue
            links.append(((down.start - up.due).days, up_slug, down_slug))
    if not links:
        return Reading(
            kpi_id="critical-path-slack-days", sim_date=snap.date, value=None, as_of=snap.date,
            detail="no open dependencies on a GA chain",
        )
    slack, up_slug, down_slug = min(links)
    return Reading(
        kpi_id="critical-path-slack-days", sim_date=snap.date, value=float(slack),
        as_of=snap.date, tripped=slack < SLACK_TRIP_DAYS,
        detail=f"{up_slug} due -> {down_slug} start; {len(links)} open link(s) on GA chains",
    )


def _blocked_split(frame: _Frame) -> tuple[int, int, int]:
    """(direct, transitive, open_points) — direct and transitive restricted to GA chains."""
    chain = _ga_chain(frame.stories)
    ups = _upstreams(frame.stories)
    open_stories = frame.open

    def stuck(slug: str) -> bool:
        s = open_stories.get(slug)
        return s is not None and (s.blocked or s.due < frame.date)

    direct = transitive = 0
    for slug, s in open_stories.items():
        if slug not in chain:
            continue
        if s.blocked:
            direct += s.points
        elif any(stuck(a) for a in _ancestors(slug, ups)):
            transitive += s.points
    return direct, transitive, sum(s.points for s in open_stories.values())


def _sim_blocked_share(frames: list[_Frame]) -> Reading:
    snap = frames[-1]
    direct, transitive, open_points = _blocked_split(snap)
    if open_points == 0:
        return Reading(
            kpi_id="blocked-share-pct", sim_date=snap.date, value=None, as_of=snap.date,
            detail="no open work",
        )
    value = round((direct + transitive) / open_points * 100, 2)
    shares: list[float | None] = [value]
    for frame in frames[max(0, len(frames) - BLOCKED_TRIP_DAYS):-1]:
        d, t, op = _blocked_split(frame)
        shares.append(None if op == 0 else (d + t) / op * 100)
    tripped = len(shares) == BLOCKED_TRIP_DAYS and all(
        v is not None and v > BLOCKED_TRIP_PCT for v in shares
    )
    return Reading(
        kpi_id="blocked-share-pct", sim_date=snap.date, value=value, as_of=snap.date,
        tripped=tripped,
        detail=f"direct {direct} + transitive {transitive} of {open_points} open pts on GA chains",
    )


def _spend_landed(row: SpendRow) -> date:
    """Week w covers week_start..+6 and its row lands the Monday after."""
    return row.week_start + timedelta(days=7)


def _no_spend_yet(kpi_id: str, snap: ProgramSnapshot) -> Reading:
    return Reading(
        kpi_id=kpi_id, sim_date=snap.sim_date, value=None, state="stale",
        reason="no spend row has landed yet",
    )


def _spend_freshness(snap: ProgramSnapshot, last: SpendRow) -> tuple[str, str | None]:
    age = (snap.sim_date - _spend_landed(last)).days
    if age > SPEND_STALE_AFTER_DAYS:
        return "stale", f"latest spend row (week {last.week}) is {age} days old"
    return "ok", None


def _sim_cost_vs_envelope(snap: ProgramSnapshot) -> Reading:
    rows = sorted(snap.spend, key=lambda r: r.week)
    if not rows:
        return _no_spend_yet("cost-vs-envelope", snap)
    ratios: list[float] = []
    cum_actual = cum_plan = 0.0
    for r in rows:
        cum_actual += r.actual_usd
        cum_plan += r.planned_usd
        ratios.append(cum_actual / cum_plan)
    tripped = len(ratios) >= 2 and all(r > COST_TRIP_RATIO for r in ratios[-2:])
    last = rows[-1]
    state, reason = _spend_freshness(snap, last)
    return Reading(
        kpi_id="cost-vs-envelope", sim_date=snap.sim_date,
        value=round(cum_actual - cum_plan, 2), state=state, reason=reason, tripped=tripped,
        as_of=_spend_landed(last),
        detail=f"${cum_actual:,.0f} actual vs ${cum_plan:,.0f} plan through week {last.week} "
        f"({ratios[-1] * 100:.1f} % of plan-to-date)",
    )


def _sim_weekly_burn(snap: ProgramSnapshot) -> Reading:
    rows = sorted(snap.spend, key=lambda r: r.week)
    if not rows:
        return _no_spend_yet("weekly-spend-burn-ratio", snap)
    ratios = [r.actual_usd / r.planned_usd for r in rows]
    tripped = ratios[-1] > BURN_TRIP_SINGLE or (
        len(ratios) >= 2 and all(r > BURN_TRIP_CONSECUTIVE for r in ratios[-2:])
    )
    last = rows[-1]
    state, reason = _spend_freshness(snap, last)
    return Reading(
        kpi_id="weekly-spend-burn-ratio", sim_date=snap.sim_date, value=round(ratios[-1], 4),
        state=state, reason=reason, tripped=tripped, as_of=_spend_landed(last),
        detail=f"week {last.week}: ${last.actual_usd:,.0f} actual vs ${last.planned_usd:,.0f} plan",
    )


_SIM_JIRA: dict[str, Callable[[list[_Frame]], Reading]] = {
    "forecast-slip-days": _sim_forecast_slip,
    "scope-change-pct": _sim_scope_change,
    "critical-path-slack-days": _sim_critical_path_slack,
    "blocked-share-pct": _sim_blocked_share,
}
_SIM_SPEND: dict[str, Callable[[ProgramSnapshot], Reading]] = {
    "cost-vs-envelope": _sim_cost_vs_envelope,
    "weekly-spend-burn-ratio": _sim_weekly_burn,
}


def _simulated(kpi_id: str) -> Measure:
    jira_formula = _SIM_JIRA.get(kpi_id)
    spend_formula = _SIM_SPEND.get(kpi_id)

    def measure(program: Program, series: list[ProgramSnapshot]) -> Reading:
        snap = series[-1]
        by_day: dict[int, ProgramSnapshot] = {}
        for s in series:
            if s.sim_day is None:
                return _broken(
                    kpi_id, snap, "a snapshot carries no sim-day: the program has no clock"
                )
            by_day[s.sim_day] = s  # the latest run for a day wins
        last = snap.sim_day
        missing = [d for d in range(last + 1) if d not in by_day]
        if missing:
            shown = ", ".join(str(d) for d in missing[:5]) + ("…" if len(missing) > 5 else "")
            return _broken(
                kpi_id, snap, f"snapshot series has gaps — no snapshot for day(s) {shown}"
            )
        days = [by_day[d] for d in range(last + 1)]
        if spend_formula is not None:
            return spend_formula(days[-1])
        frames = [_frame(s) for s in days]
        good = _last_good(frames)
        if good < last:
            # The rubric's honesty rule: a broken source carries the last good
            # reading with its date, never a number computed from an absence.
            carried = jira_formula(frames[: good + 1])
            return carried.model_copy(
                update={
                    "sim_date": snap.sim_date,
                    "state": "broken",
                    "reason": (
                        f"source broken since day {good + 1}: {len(frames[good].stories)} "
                        f"stories under the program on day {good}, "
                        f"{len(frames[last].stories)} today; carrying day {good}"
                    ),
                }
            )
        return jira_formula(frames)

    measure.__name__ = f"simulated:{kpi_id}"
    return measure


MEASURES: dict[str, Measure] = {
    "gated-pass-rate": gated_pass_rate,
    "cost-per-verified-case": cost_per_verified_case,
    "real-cost-per-run": real_cost_per_run,
    "measurement-freshness-days": measurement_freshness_days,
    "error-rate": error_rate,
    "cost-per-run-by-model": cost_per_run_by_model,
    **{
        k: _simulated(k)
        for k in (
            "forecast-slip-days", "cost-vs-envelope", "scope-change-pct",
            "critical-path-slack-days", "blocked-share-pct", "weekly-spend-burn-ratio",
        )
    },
}


def measure(kpi_id: str, program: Program, series: list[ProgramSnapshot]) -> Reading:
    try:
        fn = MEASURES[kpi_id]
    except KeyError:
        raise KeyError(f"no measure registered for {kpi_id!r}") from None
    return fn(program, series)


def source_missing(snap: ProgramSnapshot) -> ProgramSnapshot:
    """The same day with every source gone: Jira unreadable, no spend rows,
    no eval runs. What a confirmed measure must read as broken or stale."""
    health = [
        SourceHealth(source=h.source, status="error", detail="planted: source unreadable")
        if h.source != "clock"
        else h
        for h in snap.health
    ]
    return snap.model_copy(
        update={"jira": None, "spend": [], "eval_runs": [], "billing": [], "health": health}
    )
