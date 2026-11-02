"""`python -m simulate` — seed, tick, jump, verify, status, teardown (RC1-299).

Exit codes: 0 done; 1 `verify` found Jira out of step with the scenario, or a
tick was refused (no seed, or the program is over); 2 a Jira error; 3 a
converge mutated Jira and then failed, so the world is part-way between two
days (RC1-417). A converge that fails gets one retry before that verdict
(RC1-584), because converge is idempotent and most of these failures are a
timed-out read, not a broken world.

**1 and 3 are different failures and the daily job depends on it.** A 1 is
routine — the program ends on day 69 and every tick after that is refused. A 3
means the world is dirty: the clock is still on the last day that fully
landed, `converged: false` is recorded, and nothing should read the simulated
Jira as though it were any particular day until a later tick re-converges
(converge is idempotent, so the next one repairs it).

Needs JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN (config reads .env). The
board id for story points and the state directory are KPI_SIM_BOARD_ID and
KPI_SIM_DIR, defaulting to the PMA scrum board and data/kpi-sim.

`ledger` (RC1-300) needs no Jira: it derives the ground-truth ledger from the
scenario and prints it, or writes the CSV with `--out`.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from config import settings
from seed.jira_client import JiraClient, JiraError
from simulate import apply, ledger, scenario
from simulate.clock import SimState


def _jira() -> JiraClient:
    return JiraClient(
        settings.jira_base_url, settings.jira_email or "", settings.jira_api_token or ""
    )


#: A converge that mutated Jira and then failed — see the module docstring.
EXIT_DIRTY = 3


#: Seconds to wait before the one retry a failed converge gets (RC1-584).
RETRY_PAUSE_S = 20


def _converge_to(jira: JiraClient, state: SimState, day: int, *, dry_run: bool) -> int:
    try:
        report = _converge_once(jira, day, dry_run=dry_run)
    except apply.ConvergeError as first:
        # RC1-584: converge is idempotent, so a transient failure gets one retry
        # before the world is declared dirty. Without it a single timed-out read
        # costs a whole day of KPI measurement, and on 2026-11-01 it did: the
        # 07:00 job woke in a 2-second DarkWake, the one action it had to apply
        # (`p-ga -> Done`) landed in Jira but its confirming read timed out, and
        # the day was recorded unreadable even though Jira was by then exactly
        # at day 67. A retry would have found it in sync and written the clock.
        #
        # Note what `applied` does and does not tell us. It excludes the action
        # that raised, so "0 action(s)" means the first action was unconfirmed,
        # NOT that Jira is untouched — that action may well have landed. This is
        # why the retry, rather than a shortcut around mark_incomplete, is the
        # fix: only a clean converge proves what day the world is on.
        print(
            f"day {day:>2} ({scenario.sim_date(day)}): converge failed after "
            f"{len(first.applied)} confirmed action(s) — retrying once in "
            f"{RETRY_PAUSE_S}s, because converge is idempotent and a timeout is "
            f"usually transient. Cause: {first.__cause__}",
            file=sys.stderr,
        )
        time.sleep(RETRY_PAUSE_S)
        try:
            report = _converge_once(jira, day, dry_run=dry_run)
        except apply.ConvergeError as exc:
            return _mark_dirty(state, day, exc, dry_run=dry_run)
    if not dry_run:
        state.write(day, report.keys)
    n = len(report.actions)
    print(f"day {day:>2} ({scenario.sim_date(day)}, week {scenario.week_of(day)}): "
          f"{n} action(s){' [dry-run]' if dry_run else ''}")
    return 0


def _converge_once(jira: JiraClient, day: int, *, dry_run: bool) -> apply.Report:
    return apply.converge(jira, day, board_id=settings.kpi_sim_board_id, dry_run=dry_run)


def _mark_dirty(
    state: SimState, day: int, exc: apply.ConvergeError, *, dry_run: bool
) -> int:
    """The RC1-417 guard: a converge that failed twice leaves a world no day
    number describes, so record it unreadable rather than mis-date it."""
    if not dry_run:
        state.mark_incomplete(day, exc.keys)
    print(
        f"day {day:>2} ({scenario.sim_date(day)}): CONVERGE FAILED after "
        f"{len(exc.applied)} action(s) and a retry — Jira is part-way between "
        f"day {day - 1} and day {day}, and the clock stays on the last day that "
        f"fully landed. Nothing should date a snapshot until a tick re-converges "
        f"(converge is idempotent). Cause: {exc.__cause__}",
        file=sys.stderr,
    )
    return EXIT_DIRTY


def cmd_seed(args, jira, state) -> int:
    return _converge_to(jira, state, 0, dry_run=args.dry_run)


def cmd_tick(args, jira, state) -> int:
    clock = state.read_clock()
    if clock is None:
        print("no clock — run `seed` first", file=sys.stderr)
        return 1
    if not clock.converged:
        print(
            f"clock is dirty: a converge to day {clock.converging_to} failed part-way. "
            f"Re-converging from day {clock.day}.",
            file=sys.stderr,
        )
    day = clock.day
    for _ in range(args.days):
        if day >= scenario.LAST_DAY:
            print(f"day {day} is the program's last day — nothing to advance", file=sys.stderr)
            return 1
        day += 1
        # Stop on a dirty converge: walking on would stack a second half-applied
        # day on the first, and the repair is a re-converge of *this* day.
        rc = _converge_to(jira, state, day, dry_run=args.dry_run)
        if rc:
            return rc
    return 0


def cmd_to_day(args, jira, state) -> int:
    return _converge_to(jira, state, args.day, dry_run=args.dry_run)


def cmd_verify(args, jira, state) -> int:
    day = args.day
    if day is None:
        clock = state.read_clock()
        if clock is None:
            print("no clock — pass --day or run `seed` first", file=sys.stderr)
            return 1
        day = clock.day
    outstanding = apply.verify(jira, day)
    if not outstanding:
        print(f"day {day}: Jira matches the scenario")
        return 0
    print(f"day {day}: {len(outstanding)} difference(s) from the scenario:")
    for a in outstanding:
        print(f"  {a}")
    return 1


def cmd_status(args, jira, state) -> int:
    clock = state.read_clock()
    if clock is None:
        print("not seeded")
        return 0
    st = scenario.state_at(clock.day)
    done = sum(1 for i in st.issues.values() if i.status == scenario.STATUS_DONE)
    print(
        f"day {clock.day} · {clock.sim_date} · week {scenario.week_of(clock.day)} · "
        f"{len(st.issues)} stories, {done} done, {st.points_done}/{st.points_total} pts · "
        f"spend rows through week {len(st.spend)} · "
        f"events: {', '.join(e.id for e in st.events) or 'none'} · "
        f"source {'BROKEN' if scenario.source_broken_on(clock.day) else 'ok'} · "
        f"last tick {clock.updated_at}"
    )
    return 0


def cmd_teardown(args, jira, state) -> int:
    apply.teardown(jira, dry_run=args.dry_run)
    if not args.dry_run:
        state.forget()
        print("forgot the clock, manifest and spend line")
    return 0


def cmd_ledger(args) -> int:
    book = ledger.derive()
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(ledger.to_csv(book))
        print(f"wrote {args.out}: {len(book.rows)} rows, days 0-{book.days[-1]}")
        return 0
    days = range(book.days[-1] + 1) if args.day is None else [args.day]
    print(ledger.render_table(book, days))
    if args.day is not None:
        for e in book.readings(args.day):
            flag = f" [{e.state}: {e.reason}]" if e.state != "ok" else ""
            trip = " TRIPPED" if e.tripped else ""
            shown = "-" if e.value is None else e.value
            print(f"  {e.kpi_id:<26} {shown:>8}{trip}{flag}  {e.detail}")
    else:
        print("\n? stale  ! broken  * tripped")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m simulate", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def dry(p):
        p.add_argument("--dry-run", action="store_true", help="print actions, make no changes")

    dry(sub.add_parser("seed", help="create / converge the program to day 0"))
    p = sub.add_parser("tick", help="advance the clock one day and converge")
    p.add_argument("--days", type=int, default=1, help="advance this many days (default 1)")
    dry(p)
    p = sub.add_parser("to-day", help="jump to a sim-day and converge (development)")
    p.add_argument("day", type=int)
    dry(p)
    p = sub.add_parser("verify", help="does Jira match the scenario for the current day?")
    p.add_argument("--day", type=int, default=None)
    sub.add_parser("status", help="show the clock")
    dry(sub.add_parser("teardown", help="delete every simulated issue and forget the clock"))
    p = sub.add_parser("ledger", help="the ground-truth ledger, derived from the scenario")
    p.add_argument("--out", type=Path, default=None, help="write the CSV here instead")
    p.add_argument("--day", type=int, default=None, help="one day, with the working shown")
    args = ap.parse_args(argv)
    if args.cmd == "ledger":
        return cmd_ledger(args)

    handlers = {
        "seed": cmd_seed, "tick": cmd_tick, "to-day": cmd_to_day, "verify": cmd_verify,
        "status": cmd_status, "teardown": cmd_teardown,
    }
    state = SimState(Path(settings.kpi_sim_dir))
    try:
        with _jira() as jira:
            return handlers[args.cmd](args, jira, state)
    except JiraError as e:
        print(f"Jira error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
