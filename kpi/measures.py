"""Measures: a computation per confirmed KPI over a snapshot series (RC1-303).

The instrument stage's "confirmed" is only worth something if a computation
exists and runs — so this is the registry the stage checks against, and the
registry the track stage (RC1-305) will schedule. A measure takes the
program and its stored snapshots, oldest first, and returns a `Reading` for
the latest one. Numbers are computed here, by code; nothing in this module
calls a model.

Two families. The eval-run-store KPIs are implemented here over the
snapshot's run rows. The simulated program's six delegate to the ledger's
formulas (`simulate.ledger`), which are the adopted tree's definitions
already written and already proven against the scenario; they are imported
lazily because `simulate` is a development package, not a shipped one —
the track stage moves the formulas into `kpi/` when it takes them over.

`source_missing` is the instrument stage's planted-break check: the latest
snapshot with every source gone. Every confirmed measure is run against it
and must read `broken` or `stale` with a reason — never a number. That is
the rubric's staleness rule, verified per KPI rather than promised.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

from collectors.models import EvalRunRow, ProgramSnapshot, SourceHealth
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


def _freshness(age: int, reason: str) -> tuple[str, str | None]:
    return ("stale", reason) if age > FRESH_DAYS else ("ok", None)


def _previous_value(
    program: Program, series: list[ProgramSnapshot], measure: Measure
) -> float | None:
    """The same measure one snapshot earlier, for "two consecutive" rules."""
    if len(series) < 2:
        return None
    return measure(program, series[:-1]).value


# --- the simulated program: the ledger's formulas ----------------------------------------


def _simulated(kpi_id: str) -> Measure:
    def measure(program: Program, series: list[ProgramSnapshot]) -> Reading:
        from simulate import ledger  # development package; see the module docstring

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
        book = ledger.derive(
            series=[ledger.snapshot_from_collected(by_day[d]) for d in range(last + 1)]
        )
        return book.reading(last, kpi_id)

    measure.__name__ = f"simulated:{kpi_id}"
    return measure


MEASURES: dict[str, Measure] = {
    "gated-pass-rate": gated_pass_rate,
    "cost-per-verified-case": cost_per_verified_case,
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
    return snap.model_copy(update={"jira": None, "spend": [], "eval_runs": [], "health": health})
