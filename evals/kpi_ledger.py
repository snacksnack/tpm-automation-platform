"""The `kpi-ledger` subject: a KPI implementation against the ground truth (RC1-300).

One case per sim-day, seventy in all. Each case hands the implementation a
day and scores what it reads against `simulate.ledger` — the adopted tree's
formulas applied to the scenario the simulator converges Jira to. Free and
deterministic, so it gates CI the way `drift-digest-allclear` does.

What a case asserts, per KPI, as one characteristic named after the KPI:
the value is within the ledger's tolerance (or both sides have no value),
the state is the same word, and the so-what threshold agrees. Three things
in one characteristic rather than eighteen per case, because a failure is
read per KPI — "forecast is wrong on day 43" — and the detail says which of
the three it was.

And on the day after each planted event, one more: `detects-<event>`. The
event's first-day detector must read tripped (or, for the source break,
broken). This is a subset of the per-KPI check by construction, and it is
named separately on purpose: "the number was right and nobody noticed" and
"the number was wrong" are different failures with different fixes.

The implementation under test is pluggable (`IMPLEMENTATIONS`). Until the
track stage lands (RC1-305) the reference is the ledger's own derivation,
which makes the reference run a tautology — the story's done-when is that
a *wrong* implementation fails, and two are shipped: one that trusts an
empty snapshot (no source-break detection, the zero-for-unknown failure
the rubric exists to prevent) and one that forecasts over the 28-day window
the review rejected. Runs of those are never recorded; the store holds
measurements, not demonstrations.

    python -m evals run kpi-ledger                              # reference, recorded
    python -m evals run kpi-ledger --impl no-break-detection    # fails days 43-47
    python -m evals run kpi-ledger --impl window-28             # fails where the forecast differs
"""

from __future__ import annotations

import time
from collections.abc import Callable

from agent_evals.case import Case
from agent_evals.record import CaseResult, CharacteristicResult, SubjectVersion, Usage

from kpi.reading import Reading
from simulate import ledger, scenario

NAME = "kpi-ledger"

#: An implementation is anything that, given the full program, can answer
#: "what does each KPI read on day N" — the shape the track stage will have
#: once it reads snapshots instead of the scenario.
Implementation = Callable[[int], list[Reading]]

REFERENCE = "reference"


def _impl(book: ledger.Ledger) -> Implementation:
    return book.readings


IMPLEMENTATIONS: dict[str, Callable[[], Implementation]] = {
    REFERENCE: lambda: _impl(ledger.derive()),
    "no-break-detection": lambda: _impl(ledger.derive(detect_source_break=False)),
    "window-28": lambda: _impl(ledger.derive(window_days=28)),
}


def _cases() -> tuple[Case, ...]:
    detect_on = {e.day + 1: e for e in scenario.EVENTS}
    out = []
    for day in range(scenario.LAST_DAY + 1):
        expect = list(ledger.KPI_IDS)
        tags = [NAME, f"week-{scenario.week_of(day)}"]
        if day in detect_on:
            expect.append(f"detects-{detect_on[day].id}")
        tags += [e.id for e in scenario.active_events(day)]
        out.append(
            Case(id=f"day-{day:02d}", input={"day": day}, expect=tuple(expect), tags=tuple(tags))
        )
    return tuple(out)


CASES = _cases()


def version(impl: str) -> SubjectVersion:
    from evals.subjects import _code_version

    return SubjectVersion(
        subject=NAME,
        code_version=_code_version(),
        # Deterministic: there is no model, and the ledger's formulas are the
        # "prompt" — their version is the package's.
        model=None,
        prompt_version=None if impl == REFERENCE else f"impl:{impl}",
    )


# --- scoring ---------------------------------------------------------------------------------


def compare(expected: ledger.Row, got: Reading | None) -> CharacteristicResult:
    """One KPI on one day: value within tolerance, same state, same tripped."""
    e = expected.expected
    if got is None:
        return CharacteristicResult(name=e.kpi_id, passed=False, detail="no reading produced")
    problems = []
    if (e.value is None) != (got.value is None):
        problems.append(f"value {got.value} vs expected {e.value}")
    elif e.value is not None and abs(got.value - e.value) > expected.tolerance:
        problems.append(
            f"value {got.value} vs expected {e.value} (±{expected.tolerance:g} {expected.unit})"
        )
    if got.state != e.state:
        problems.append(f"state {got.state!r} vs expected {e.state!r}")
    if got.tripped != e.tripped:
        problems.append(f"tripped {got.tripped} vs expected {e.tripped}")
    if problems:
        return CharacteristicResult(name=e.kpi_id, passed=False, detail="; ".join(problems))
    shown = "-" if e.value is None else f"{e.value:g}"
    mark = "" if e.state == "ok" else f" [{e.state}]"
    return CharacteristicResult(
        name=e.kpi_id, passed=True,
        detail=f"{shown}{mark}{' tripped' if e.tripped else ''}",
    )


def detects(event: scenario.Event, got: dict[str, Reading]) -> CharacteristicResult:
    """The event's first-day detector shows its signal on the day after."""
    missed = []
    for kpi_id in event.detector:
        r = got.get(kpi_id)
        if r is None:
            missed.append(f"{kpi_id}: no reading")
        elif event.signal == "broken" and r.state != "broken":
            missed.append(f"{kpi_id}: state {r.state!r}, not broken")
        elif event.signal == "tripped" and not r.tripped:
            missed.append(f"{kpi_id}: not tripped (value {r.value})")
    return CharacteristicResult(
        name=f"detects-{event.id}",
        passed=not missed,
        detail=(
            f"{', '.join(event.detector)} read {event.signal} by day {event.day + 1}"
            if not missed
            else "; ".join(missed)
        ),
    )


def run_case(case: Case, impl: Implementation, truth: ledger.Ledger) -> CaseResult:
    day = case.input["day"]
    started = time.perf_counter()
    try:
        got = {r.kpi_id: r for r in impl(day)}
    except Exception as exc:
        return CaseResult(
            case_id=case.id,
            usage=Usage(latency_ms=(time.perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )
    latency_ms = (time.perf_counter() - started) * 1000

    results = [compare(truth.row(day, kpi_id), got.get(kpi_id)) for kpi_id in ledger.KPI_IDS]
    for event in scenario.EVENTS:
        if day == event.day + 1:
            results.append(detects(event, got))
    return CaseResult(
        case_id=case.id,
        characteristics=results,
        usage=Usage(latency_ms=latency_ms),
        observations={
            "sim_date": scenario.sim_date(day).isoformat(),
            "active_events": [e.id for e in scenario.active_events(day)],
            "readings": {
                k: {"value": r.value, "state": r.state, "tripped": r.tripped}
                for k, r in sorted(got.items())
            },
            "mismatches": sum(1 for c in results if not c.passed),
        },
    )


def run(impl_name: str = REFERENCE, cases=CASES) -> list[CaseResult]:
    impl = IMPLEMENTATIONS[impl_name]()
    truth = ledger.derive()
    return [run_case(case, impl, truth) for case in cases]
