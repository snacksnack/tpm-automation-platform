"""The ground-truth ledger: what every KPI should read on every sim-day (RC1-300).

Derived, never typed. `derive()` walks `scenario.state_at(day)` for days 0..69
the way the collector (RC1-301) will walk Jira — seeing only the issues that
carry the program label, with yesterday's snapshot beside today's — and
applies the adopted tree's formulas (docs/kpi/trees/simulated-program.review.md)
to produce one `Reading` per KPI per day, plus the tolerance the eval may
allow. A scenario edit changes the ledger on the next derive; a formula
disagreement shows up as a diff in the committed CSV.

Two things the ledger is strict about, because they are the rubric's whole
point:

* **No value is ever zero for "unknown".** Before the first spend row lands
  the cost KPIs read `stale` with a reason; during the week-7 source break the
  Jira KPIs read `broken` and carry day 42's value *with day 42's date*. An
  implementation that reports 0 % scope change on an empty snapshot fails
  every case from day 43 to 47 — that is the deliberately wrong
  implementation the story's done-when names (`detect_source_break=False`).
* **Tripped is part of the reading.** Each KPI's so-what threshold is
  evaluated here, so "the number was right but nobody noticed" is a failure
  the suite can name: the event's first-day detector must read tripped (or
  broken) by the day after the event.

Decisions the tree left open, made here (and written up in docs/kpi/ledger.md):
links whose upstream or downstream is Done drop out of the slack minimum;
blocked share's numerator is restricted to GA-chain stories and its
denominator is all open points; "no value yet" is `stale`; a chain is
"to a GA-blocking story" by the `ga-blocking` label, which is how a snapshot
— not the scenario — identifies the root.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from collectors.models import ProgramSnapshot
from kpi.reading import Reading
from simulate import scenario
from simulate.scenario import (
    GA_BLOCKING_LABEL,
    GA_DAY,
    LAST_DAY,
    PROGRAM_LABEL,
    STATUS_BLOCKED,
    STATUS_DONE,
    IssueState,
    SpendRow,
)

WINDOW_DAYS = 14  # trailing throughput window (the review kept 14 over the agent's 28)
PLAN_DAYS = 70  # the plan rate: total points over the ten-week program
SOURCE_BREAK_DROP = 0.5  # issue count falling below this share of yesterday's = broken


@dataclass(frozen=True)
class Spec:
    id: str
    unit: str
    tolerance: float
    source: str  # "jira" | "spend"
    stale_after_days: int
    so_what: str


SPECS: tuple[Spec, ...] = (
    Spec("forecast-slip-days", "days", 1.0, "jira", 2, "> +5 days"),
    Spec("cost-vs-envelope", "USD over plan-to-date", 1.0, "spend", 8,
         "> 110 % of plan-to-date for two consecutive weeks"),
    Spec("scope-change-pct", "% of baseline points", 0.5, "jira", 2, "net > +10 %"),
    Spec("critical-path-slack-days", "days", 0.5, "jira", 2, "< 3 days on a GA chain"),
    Spec("blocked-share-pct", "% of open points", 1.0, "jira", 2,
         "> 25 % for three consecutive days"),
    Spec("weekly-spend-burn-ratio", "actual / plan, latest week", 0.01, "spend", 8,
         "> 1.5 in a week, or > 1.2 for two consecutive weeks"),
)
BY_ID: dict[str, Spec] = {s.id: s for s in SPECS}
KPI_IDS: tuple[str, ...] = tuple(s.id for s in SPECS)
JIRA_KPIS: tuple[str, ...] = tuple(s.id for s in SPECS if s.source == "jira")
SPEND_KPIS: tuple[str, ...] = tuple(s.id for s in SPECS if s.source == "spend")

FORECAST_TRIP_DAYS = 5.0
COST_TRIP_RATIO = 1.10
SCOPE_TRIP_PCT = 10.0
SLACK_TRIP_DAYS = 3.0
BLOCKED_TRIP_PCT = 25.0
BLOCKED_TRIP_DAYS = 3
BURN_TRIP_SINGLE = 1.5
BURN_TRIP_CONSECUTIVE = 1.2


# --- what the collector sees ----------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """One day as the collector will report it: only issues under the program
    label (the source break makes them vanish), the spend rows that have landed,
    and the committed GA date."""

    day: int
    issues: dict[str, IssueState]
    spend: list[SpendRow]
    ga_day: int = GA_DAY

    @property
    def date(self) -> date:
        return scenario.sim_date(self.day)

    @property
    def open(self) -> dict[str, IssueState]:
        return {s: i for s, i in self.issues.items() if i.status != STATUS_DONE}

    @property
    def total_points(self) -> int:
        return sum(i.points for i in self.issues.values())


def snapshot(day: int) -> Snapshot:
    st = scenario.state_at(day)
    visible = {slug: i for slug, i in st.issues.items() if PROGRAM_LABEL in i.labels}
    return Snapshot(day=day, issues=visible, spend=list(st.spend))


def _label_value(labels: Iterable[str], prefix: str) -> str:
    return next((lb[len(prefix):] for lb in labels if lb.startswith(prefix)), "")


def snapshot_from_collected(collected: ProgramSnapshot) -> Snapshot:
    """A collected program snapshot (RC1-301) in the ledger's shape, so the
    same formulas run over what the collector stored as over the scenario.

    Issues are keyed by their `ks-` slug label where they have one (the
    simulated program's stories do), else by Jira key, so a collected series
    and the scenario line up story for story. The epic is not a story: it is
    read for the committed GA date and dropped. A source that errored
    (`jira=None`) is an empty snapshot — the source-break rule sees the count
    fall, which is the reading the tree asks for.
    """
    if collected.sim_day is None:
        raise ValueError("a collected snapshot needs a sim_day to join the ledger's series")
    issues: dict[str, IssueState] = {}
    ga_day = GA_DAY
    project = collected.jira
    if project is not None:
        key_to_slug = {
            i.key: (_label_value(i.labels, scenario.SLUG_PREFIX) or i.key) for i in project.issues
        }
        blocks: dict[str, list[str]] = {}
        for link in project.links:
            if link.upstream in key_to_slug and link.downstream in key_to_slug:
                blocks.setdefault(key_to_slug[link.upstream], []).append(
                    key_to_slug[link.downstream]
                )
        for i in project.issues:
            if i.issue_type == "Epic":
                if i.due is not None:
                    ga_day = (i.due - scenario.KICKOFF).days
                continue
            if i.start is None or i.due is None:
                continue  # a story with no dates cannot sit on a schedule; reported by RC1-303
            slug = key_to_slug[i.key]
            issues[slug] = IssueState(
                slug=slug,
                summary=i.summary,
                status=i.status,
                start=i.start,
                due=i.due,
                points=int(i.points or 0),
                labels=frozenset(i.labels),
                blocks=tuple(sorted(blocks.get(slug, ()))),
                owner=_label_value(i.labels, "own-"),
                workstream=_label_value(i.labels, "ws-"),
                created_day=(i.created - scenario.KICKOFF).days if i.created else 0,
            )
    spend = [
        SpendRow(
            week=r.week, planned_usd=r.planned_usd, actual_usd=r.actual_usd,
            lands_on_day=r.landed_on_day if r.landed_on_day is not None else 7 * r.week,
        )
        for r in collected.spend
    ]
    return Snapshot(day=collected.sim_day, issues=issues, spend=spend, ga_day=ga_day)


# --- the graph on a day ------------------------------------------------------------------


def _upstreams(snap: Snapshot) -> dict[str, set[str]]:
    """slug -> slugs that block it, within the snapshot."""
    ups: dict[str, set[str]] = {s: set() for s in snap.issues}
    for slug, issue in snap.issues.items():
        for down in issue.blocks:
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


def ga_chain(snap: Snapshot) -> set[str]:
    """Every story on a chain to a GA-blocking story, roots included."""
    ups = _upstreams(snap)
    roots = {s for s, i in snap.issues.items() if GA_BLOCKING_LABEL in i.labels}
    chain = set(roots)
    for r in roots:
        chain |= _ancestors(r, ups)
    return chain


# --- the formulas -----------------------------------------------------------------------


def _points_done_between(then: Snapshot, now: Snapshot) -> int:
    """Points on issues Done now that were not Done (or not present) then."""
    return sum(
        i.points
        for slug, i in now.issues.items()
        if i.status == STATUS_DONE
        and (slug not in then.issues or then.issues[slug].status != STATUS_DONE)
    )


def forecast_slip(
    series: list[Snapshot], day: int, *, window_days: int = WINDOW_DAYS
) -> Reading:
    snap = series[day]
    when = snap.date
    remaining = sum(i.points for i in snap.open.values())
    if remaining == 0:
        # The first day of the *current* all-Done stretch, not the first empty
        # snapshot ever: a source break also reads as zero remaining.
        completed = day
        while completed > 0 and not series[completed - 1].open and series[completed - 1].issues:
            completed -= 1
        value = float(completed - snap.ga_day)
        return Reading(
            kpi_id="forecast-slip-days", sim_date=when, value=value, as_of=when,
            tripped=value > FORECAST_TRIP_DAYS,
            detail=f"all work Done on day {completed}; committed GA day {snap.ga_day}",
        )
    if day >= window_days:
        done = _points_done_between(series[day - window_days], snap)
        rate = done / window_days
        basis = f"{done} pts Done in the last {window_days} days"
    else:
        rate = snap.total_points / PLAN_DAYS
        basis = (
            f"plan rate {snap.total_points} pts / {PLAN_DAYS} days "
            f"(under {window_days} days of history)"
        )
    if rate == 0:
        return Reading(
            kpi_id="forecast-slip-days", sim_date=when, value=None, state="broken",
            reason="no completed work to forecast from", as_of=when,
            detail=f"{remaining} pts remaining; {basis}",
        )
    forecast_day = day + remaining / rate
    value = round(forecast_day - snap.ga_day, 2)
    return Reading(
        kpi_id="forecast-slip-days", sim_date=when, value=value, as_of=when,
        tripped=value > FORECAST_TRIP_DAYS,
        detail=(
            f"{remaining} pts remaining at {rate:.3f} pts/day ({basis}) -> day {forecast_day:.1f}"
        ),
    )


def scope_change(series: list[Snapshot], day: int) -> Reading:
    snap, base = series[day], series[0]
    baseline = base.total_points
    added = sum(i.points for s, i in snap.issues.items() if s not in base.issues)
    removed = sum(i.points for s, i in base.issues.items() if s not in snap.issues)
    value = round((added - removed) / baseline * 100, 2)
    return Reading(
        kpi_id="scope-change-pct", sim_date=snap.date, value=value, as_of=snap.date,
        tripped=value > SCOPE_TRIP_PCT,
        detail=f"+{added} / -{removed} pts against a {baseline}-pt baseline",
    )


def critical_path_slack(series: list[Snapshot], day: int) -> Reading:
    snap = series[day]
    chain = ga_chain(snap)
    links: list[tuple[int, str, str]] = []
    for up_slug, up in snap.issues.items():
        if up.status == STATUS_DONE:
            continue  # a delivered upstream cannot consume slack
        for down_slug in up.blocks:
            down = snap.issues.get(down_slug)
            if down is None or down_slug not in chain or down.status == STATUS_DONE:
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


def _blocked_points(snap: Snapshot) -> tuple[int, int, int]:
    """(direct, transitive, open_points) — direct and transitive restricted to GA chains."""
    chain = ga_chain(snap)
    ups = _upstreams(snap)
    open_issues = snap.open
    today = snap.date

    def stuck(slug: str) -> bool:
        i = open_issues.get(slug)
        return i is not None and (i.status == STATUS_BLOCKED or i.due < today)

    direct = transitive = 0
    for slug, i in open_issues.items():
        if slug not in chain:
            continue
        if i.status == STATUS_BLOCKED:
            direct += i.points
        elif any(stuck(a) for a in _ancestors(slug, ups)):
            transitive += i.points
    return direct, transitive, sum(i.points for i in open_issues.values())


def blocked_share(series: list[Snapshot], day: int) -> Reading:
    snap = series[day]
    direct, transitive, open_points = _blocked_points(snap)
    if open_points == 0:
        return Reading(
            kpi_id="blocked-share-pct", sim_date=snap.date, value=None, as_of=snap.date,
            detail="no open work",
        )
    value = round((direct + transitive) / open_points * 100, 2)
    recent = [value] + [
        _share(series[d]) for d in range(day - 1, max(-1, day - BLOCKED_TRIP_DAYS), -1)
    ]
    tripped = len(recent) == BLOCKED_TRIP_DAYS and all(
        v is not None and v > BLOCKED_TRIP_PCT for v in recent
    )
    return Reading(
        kpi_id="blocked-share-pct", sim_date=snap.date, value=value, as_of=snap.date,
        tripped=tripped,
        detail=f"direct {direct} + transitive {transitive} of {open_points} open pts on GA chains",
    )


def _share(snap: Snapshot) -> float | None:
    direct, transitive, open_points = _blocked_points(snap)
    return None if open_points == 0 else (direct + transitive) / open_points * 100


def _no_spend_yet(kpi_id: str, snap: Snapshot) -> Reading:
    return Reading(
        kpi_id=kpi_id, sim_date=snap.date, value=None, state="stale",
        reason="no spend row has landed yet; week 1 lands on day 7",
    )


def _spend_state(spec: Spec, snap: Snapshot, last: SpendRow) -> tuple[str, str | None]:
    age = snap.day - last.lands_on_day
    if age > spec.stale_after_days:
        return "stale", f"latest spend row (week {last.week}) is {age} days old"
    return "ok", None


def cost_vs_envelope(series: list[Snapshot], day: int) -> Reading:
    snap = series[day]
    if not snap.spend:
        return _no_spend_yet("cost-vs-envelope", snap)
    cum_actual = sum(r.actual_usd for r in snap.spend)
    cum_plan = sum(r.planned_usd for r in snap.spend)
    ratios = [
        sum(r.actual_usd for r in snap.spend[: n + 1])
        / sum(r.planned_usd for r in snap.spend[: n + 1])
        for n in range(len(snap.spend))
    ]
    tripped = len(ratios) >= 2 and all(r > COST_TRIP_RATIO for r in ratios[-2:])
    last = snap.spend[-1]
    state, reason = _spend_state(BY_ID["cost-vs-envelope"], snap, last)
    return Reading(
        kpi_id="cost-vs-envelope", sim_date=snap.date, value=round(cum_actual - cum_plan, 2),
        state=state, reason=reason, tripped=tripped, as_of=scenario.sim_date(last.lands_on_day),
        detail=f"${cum_actual:,.0f} actual vs ${cum_plan:,.0f} plan through week {last.week} "
        f"({ratios[-1] * 100:.1f} % of plan-to-date)",
    )


def weekly_burn(series: list[Snapshot], day: int) -> Reading:
    snap = series[day]
    if not snap.spend:
        return _no_spend_yet("weekly-spend-burn-ratio", snap)
    ratios = [r.actual_usd / r.planned_usd for r in snap.spend]
    last = snap.spend[-1]
    tripped = ratios[-1] > BURN_TRIP_SINGLE or (
        len(ratios) >= 2 and all(r > BURN_TRIP_CONSECUTIVE for r in ratios[-2:])
    )
    state, reason = _spend_state(BY_ID["weekly-spend-burn-ratio"], snap, last)
    return Reading(
        kpi_id="weekly-spend-burn-ratio", sim_date=snap.date, value=round(ratios[-1], 4),
        state=state, reason=reason, tripped=tripped, as_of=scenario.sim_date(last.lands_on_day),
        detail=f"week {last.week}: ${last.actual_usd:,.0f} actual vs ${last.planned_usd:,.0f} plan",
    )


# --- the ledger ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    day: int
    expected: Reading
    tolerance: float
    unit: str
    active_events: tuple[str, ...]

    @property
    def kpi_id(self) -> str:
        return self.expected.kpi_id


@dataclass
class Ledger:
    rows: list[Row] = field(default_factory=list)

    def readings(self, day: int) -> list[Reading]:
        return [r.expected for r in self.rows if r.day == day]

    def row(self, day: int, kpi_id: str) -> Row:
        return next(r for r in self.rows if r.day == day and r.kpi_id == kpi_id)

    def reading(self, day: int, kpi_id: str) -> Reading:
        return self.row(day, kpi_id).expected

    @property
    def days(self) -> list[int]:
        return sorted({r.day for r in self.rows})


def _source_broken(series: list[Snapshot], day: int, last_good: int) -> bool:
    """The detection rule the tree specifies: the issue count under the program
    falls by more than half between consecutive snapshots. Stays broken until
    the count is back to at least half of the last good day's."""
    if day == 0:
        return False
    reference = len(series[last_good].issues)
    return reference > 0 and len(series[day].issues) < reference * SOURCE_BREAK_DROP


def derive(
    *,
    window_days: int = WINDOW_DAYS,
    detect_source_break: bool = True,
    last_day: int = LAST_DAY,
    series: list[Snapshot] | None = None,
) -> Ledger:
    """The ledger for days 0..`last_day`.

    `window_days` and `detect_source_break` exist so the eval can run a
    deliberately wrong implementation through the same code path: the
    agent's rejected 28-day window, and an implementation that trusts an
    empty snapshot. The reference is the defaults.

    `series` replaces the scenario with snapshots from elsewhere — the
    collector's, via `snapshot_from_collected` — one per day from day 0,
    contiguous. That is how "a day's KPI values can be recomputed from its
    snapshot alone" (RC1-301) is checked: the same formulas, a different
    source, the same ledger.
    """
    if series is None:
        series = [snapshot(d) for d in range(last_day + 1)]
    else:
        if [s.day for s in series] != list(range(len(series))):
            raise ValueError("series must be one snapshot per day from day 0, in order")
        last_day = len(series) - 1
    ledger = Ledger()
    last_good = 0
    broke_on: int | None = None
    for day in range(last_day + 1):
        snap = series[day]
        events = tuple(e.id for e in scenario.active_events(day))
        broken = detect_source_break and _source_broken(series, day, last_good)
        if broken and broke_on is None:
            broke_on = day
        if not broken:
            broke_on = None
        readings: dict[str, Reading] = {
            "forecast-slip-days": forecast_slip(series, day, window_days=window_days),
            "scope-change-pct": scope_change(series, day),
            "critical-path-slack-days": critical_path_slack(series, day),
            "blocked-share-pct": blocked_share(series, day),
            "cost-vs-envelope": cost_vs_envelope(series, day),
            "weekly-spend-burn-ratio": weekly_burn(series, day),
        }
        if broken:
            before, now = len(series[last_good].issues), len(snap.issues)
            reason = (
                f"source broken since day {broke_on}: {before} issues under the program "
                f"label on day {last_good}, {now} today; carrying day {last_good}"
            )
            for kpi_id in JIRA_KPIS:
                good = ledger.reading(last_good, kpi_id)
                readings[kpi_id] = good.model_copy(
                    update={"sim_date": snap.date, "state": "broken", "reason": reason}
                )
        else:
            last_good = day
        for kpi_id in KPI_IDS:
            spec = BY_ID[kpi_id]
            ledger.rows.append(
                Row(day, readings[kpi_id], spec.tolerance, spec.unit, events)
            )
    return ledger


# --- event reactions ------------------------------------------------------------------------


@dataclass(frozen=True)
class Reaction:
    event_id: str
    kpi_id: str
    detector: bool
    reacted_on: int | None  # first day >= event.day the reading moved; None = never
    how: str

    @property
    def lag(self) -> int | None:
        if self.reacted_on is None:
            return None
        return self.reacted_on - scenario.BY_EVENT[self.event_id].day


def _moved(before: Reading, after: Reading, tolerance: float) -> str | None:
    if after.state != before.state:
        return f"state {before.state} -> {after.state}"
    if after.tripped != before.tripped:
        return f"tripped {before.tripped} -> {after.tripped}"
    if (before.value is None) != (after.value is None):
        return f"value {before.value} -> {after.value}"
    if before.value is not None and abs(after.value - before.value) > tolerance:
        return f"value {before.value} -> {after.value}"
    return None


def reactions(ledger: Ledger, *, within: int = WINDOW_DAYS) -> list[Reaction]:
    """When each `must_move` KPI first reacts to each planted event, judged
    against the reading on the day before the event."""
    out: list[Reaction] = []
    for event in scenario.EVENTS:
        for kpi_id in event.must_move:
            before = ledger.reading(event.day - 1, kpi_id)
            tolerance = BY_ID[kpi_id].tolerance
            hit = next(
                (
                    (d, how)
                    for d in range(event.day, min(event.day + within, LAST_DAY) + 1)
                    if (how := _moved(before, ledger.reading(d, kpi_id), tolerance))
                ),
                None,
            )
            out.append(
                Reaction(
                    event.id, kpi_id, kpi_id in event.detector,
                    None if hit is None else hit[0], "" if hit is None else hit[1],
                )
            )
    return out


# --- files -----------------------------------------------------------------------------------

COLUMNS = (
    "sim_day", "sim_date", "kpi_id", "expected_value", "unit", "tolerance", "state",
    "tripped", "as_of", "active_events", "reason", "detail",
)


def to_csv(ledger: Ledger) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLUMNS)
    for r in ledger.rows:
        e = r.expected
        w.writerow(
            [
                r.day, e.sim_date.isoformat(), e.kpi_id,
                "" if e.value is None else f"{e.value:g}", r.unit, f"{r.tolerance:g}",
                e.state, str(e.tripped).lower(), e.as_of.isoformat() if e.as_of else "",
                " ".join(r.active_events), e.reason or "", e.detail,
            ]
        )
    return buf.getvalue()


def render_table(ledger: Ledger, days: Iterable[int]) -> str:
    """A compact per-day view for the terminal and the doc."""
    lines = [f"{'day':>3}  {'date':<10}  " + "  ".join(f"{k[:14]:>14}" for k in KPI_IDS)]
    for day in days:
        cells = []
        for kpi_id in KPI_IDS:
            e = ledger.reading(day, kpi_id)
            v = "-" if e.value is None else f"{e.value:g}"
            mark = {"ok": "", "stale": "?", "broken": "!"}[e.state] + ("*" if e.tripped else "")
            cells.append(f"{v + mark:>14}")
        lines.append(f"{day:>3}  {scenario.sim_date(day).isoformat():<10}  " + "  ".join(cells))
    return "\n".join(lines)
