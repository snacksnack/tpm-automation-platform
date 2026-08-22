"""`python -m simulate` — seed, tick, jump, verify, status, teardown (RC1-299).

Exit codes: 0 done; 1 `verify` found Jira out of step with the scenario, or a
tick was refused (no seed, or the program is over); 2 a Jira error.

Needs JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN (config reads .env). The
board id for story points and the state directory are KPI_SIM_BOARD_ID and
KPI_SIM_DIR, defaulting to the PMA scrum board and data/kpi-sim.

`ledger` (RC1-300) needs no Jira: it derives the ground-truth ledger from the
scenario and prints it, or writes the CSV with `--out`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from config import settings
from seed.jira_client import JiraClient, JiraError
from simulate import apply, ledger, scenario
from simulate.clock import SimState


def _jira() -> JiraClient:
    return JiraClient(
        settings.jira_base_url, settings.jira_email or "", settings.jira_api_token or ""
    )


def _converge_to(jira: JiraClient, state: SimState, day: int, *, dry_run: bool) -> int:
    report = apply.converge(jira, day, board_id=settings.kpi_sim_board_id, dry_run=dry_run)
    if not dry_run:
        state.write(day, report.keys)
    n = len(report.actions)
    print(f"day {day:>2} ({scenario.sim_date(day)}, week {scenario.week_of(day)}): "
          f"{n} action(s){' [dry-run]' if dry_run else ''}")
    return 0


def cmd_seed(args, jira, state) -> int:
    return _converge_to(jira, state, 0, dry_run=args.dry_run)


def cmd_tick(args, jira, state) -> int:
    clock = state.read_clock()
    if clock is None:
        print("no clock — run `seed` first", file=sys.stderr)
        return 1
    day = clock.day
    for _ in range(args.days):
        if day >= scenario.LAST_DAY:
            print(f"day {day} is the program's last day — nothing to advance", file=sys.stderr)
            return 1
        day += 1
        _converge_to(jira, state, day, dry_run=args.dry_run)
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
