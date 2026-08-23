"""`python -m collectors` — take, list and show program snapshots (RC1-301).

    python -m collectors snapshot simulated-program   # collect every source, store, print health
    python -m collectors runs simulated-program       # every stored run: sim-date, health
    python -m collectors show simulated-program       # the latest snapshot, --run N, --sim-date D

Exit codes for `snapshot`: 0 every source read ok; 1 a source was missing or
errored — the snapshot is stored either way, because "the source was gone on
this day" is a fact the KPI stage needs, and a run that refused to record it
would leave a gap that reads as "nobody looked".

Jira credentials come from .env via config; the eval store's DSN from
EVAL_DATABASE_URL in the process environment (never a repo .env — RC1-263).
The store is `DB_PATH` (data/drift.db by default) — the same database the
drift detector writes, on purpose.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date

from collectors import program as collect
from collectors import programs
from collectors.jira import JiraCollector, JiraError
from collectors.models import ProgramSnapshot
from config import settings
from store.snapshot_store import SnapshotStore


def _jira() -> JiraCollector | None:
    if not (settings.jira_email and settings.jira_api_token):
        return None
    return JiraCollector(settings.jira_base_url, settings.jira_email, settings.jira_api_token)


def _print_health(snap: ProgramSnapshot) -> None:
    when = f"sim-day {snap.sim_day}, " if snap.sim_day is not None else ""
    print(
        f"{snap.program_id}: {when}{snap.sim_date} "
        f"(collected {snap.collected_at:%Y-%m-%d %H:%M} UTC)"
    )
    for h in snap.health:
        mark = {"ok": " ", "missing": "?", "error": "!"}[h.status]
        print(f"  {mark} {h.source:<10} {h.status:<8} {h.count:>4}  {h.detail}")


def cmd_snapshot(args: argparse.Namespace) -> int:
    prog = programs.get(args.program)
    jira = _jira() if prog.jira else None
    try:
        snap = collect.collect_program(
            prog, jira=jira, eval_dsn=os.environ.get("EVAL_DATABASE_URL")
        )
    finally:
        if jira is not None:
            jira.__exit__(None, None, None)
    with SnapshotStore(args.db) as store:
        run_id = store.save_program_snapshot(snap, project_key=prog.project_key)
    if args.json:
        print(snap.model_dump_json(indent=2))
    else:
        _print_health(snap)
        print(f"  stored as run {run_id} in {args.db}")
    return 0 if snap.healthy else 1


def cmd_runs(args: argparse.Namespace) -> int:
    with SnapshotStore(args.db) as store:
        runs = store.program_runs(args.program)
        if not runs:
            print(f"no snapshots of {args.program!r} in {args.db}")
            return 0
        print(f"{'run':>4}  {'sim-day':>7}  {'sim-date':<10}  {'collected':<16}  sources")
        for r in runs:
            snap = store.load_program_snapshot(r.run_id)
            sources = ", ".join(
                f"{h.source}={h.status}" + (f"({h.count})" if h.status == "ok" else "")
                for h in snap.health
            )
            day = "-" if r.sim_day is None else str(r.sim_day)
            print(
                f"{r.run_id:>4}  {day:>7}  {r.sim_date}  {r.collected_at:%Y-%m-%d %H:%M}  {sources}"
            )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with SnapshotStore(args.db) as store:
        if args.run is not None:
            run_id = args.run
        else:
            sim_date = date.fromisoformat(args.sim_date) if args.sim_date else None
            latest = store.latest_program_run(args.program, sim_date=sim_date)
            if latest is None:
                print(f"no snapshot of {args.program!r}" + (f" for {sim_date}" if sim_date else ""))
                return 1
            run_id = latest.run_id
        snap = store.load_program_snapshot(run_id)
    if args.json:
        print(snap.model_dump_json(indent=2))
        return 0
    _print_health(snap)
    if snap.jira is not None:
        done = sum(1 for i in snap.jira.issues if i.status == "Done")
        points = sum(i.points or 0 for i in snap.jira.issues if i.issue_type != "Epic")
        print(
            f"  jira: {len(snap.jira.issues)} issue(s), {done} Done, {points:g} pts, "
            f"{len(snap.jira.links)} link(s)"
        )
    if snap.spend:
        last = snap.spend[-1]
        print(
            f"  spend: weeks 1-{last.week}; latest ${last.actual_usd:,.0f} "
            f"vs ${last.planned_usd:,.0f} plan"
        )
    if snap.eval_runs:
        cost = sum(r.cost_usd for r in snap.eval_runs)
        print(f"  eval-store: {len(snap.eval_runs)} run(s), ${cost:.2f} total")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m collectors", description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=settings.db_path, help="snapshot store (default: DB_PATH)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("snapshot", help="collect a program's sources and store the snapshot")
    p.add_argument("program", choices=sorted(programs.PROGRAMS))
    p.add_argument("--json", action="store_true", help="print the snapshot as JSON")
    p.set_defaults(func=cmd_snapshot)
    p = sub.add_parser("runs", help="list a program's stored snapshots")
    p.add_argument("program", choices=sorted(programs.PROGRAMS))
    p.set_defaults(func=cmd_runs)
    p = sub.add_parser("show", help="one stored snapshot")
    p.add_argument("program", choices=sorted(programs.PROGRAMS))
    p.add_argument("--run", type=int, default=None)
    p.add_argument("--sim-date", default=None, help="the latest snapshot for this sim-date")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except JiraError as exc:
        print(f"Jira error: {exc}", file=sys.stderr)
        return 2
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
