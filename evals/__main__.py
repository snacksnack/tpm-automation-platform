"""`python -m evals` — run a subject, or check the template version (RC1-261).

Exit codes are CI-shaped and match the planner repo's harness so a workflow
step reads the same in both: `0` everything passed, `1` a case failed, `2` a
case errored — meaning the subject produced nothing to score, which is a
different problem from producing something wrong.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from agent_evals.record import RunRecord, RunStore, new_run_id

from evals import subjects

RUNS_PATH = Path(os.environ.get("EVAL_RUNS_PATH", "./eval-runs/runs.jsonl"))


def _store():
    """The shared Postgres store when `EVAL_DATABASE_URL` is set, else the
    local JSONL default (RC1-263).

    Read from the process environment, never `.env` — the credential lives in
    one place outside every repo. An unreachable store fails the run loudly:
    a silent fallback to the file would fork the record history.
    """
    dsn = os.environ.get("EVAL_DATABASE_URL")
    if dsn:
        from agent_evals.sql_store import SqlRunStore

        store = SqlRunStore(dsn)
        store.ensure_schema()
        return store
    return RunStore(RUNS_PATH)

#: The template carries a hand-maintained version in an HTML comment. The eval
#: records a hash. `template-drift` is what notices when the two disagree.
_VERSION_COMMENT = re.compile(r"version\s+(\d+)", re.IGNORECASE)


def _print(result) -> None:
    if result.error:
        print(f"  ERROR {result.case_id}: {result.error}")
        return
    status = "pass" if result.passed else "FAIL"
    print(f"  {status} {result.case_id}  ({result.usage.latency_ms:.0f} ms)")
    for characteristic in result.characteristics:
        if characteristic.passed and not characteristic.advisory:
            continue
        mark = "~" if characteristic.advisory else "✗"
        tag = " [advisory]" if characteristic.advisory else ""
        print(f"    {mark} {characteristic.name}{tag}: {characteristic.detail}")


def cmd_run(args: argparse.Namespace) -> int:
    started = datetime.now(UTC)
    if args.subject == subjects.FREE_NAME:
        cases = subjects.FREE_CASES
        version = subjects.free_version()
        results = [subjects.run_free(case) for case in cases]
    elif args.subject == subjects.BILLED_NAME:
        # Resolved here, not in the library. `agent_evals` never reads an
        # environment variable — each consumer spells its own differently, and
        # this repo's is a plain ANTHROPIC_API_KEY read via config.settings
        # (which is what picks up .env).
        from config import settings

        key = settings.anthropic_api_key
        if not key:
            print(
                "ANTHROPIC_API_KEY is not set (config reads .env). This subject drives a real "
                f"model over the shipped template; `{subjects.FREE_NAME}` is the free half and "
                "needs no key.",
                file=sys.stderr,
            )
            return 2
        import anthropic

        client = anthropic.Anthropic(api_key=key, timeout=60.0, max_retries=3)
        model = args.model
        cases = subjects.BILLED_CASES
        version = subjects.billed_version(model)
        print(f"{len(cases)} case(s) against {model} — this spends money.")
        results = [subjects.run_billed(case, client, model) for case in cases]
    else:
        print(f"unknown subject {args.subject!r}", file=sys.stderr)
        return 2

    print(f"\n{args.subject}")
    for result in results:
        _print(result)

    record = RunRecord(
        run_id=new_run_id(args.subject),
        subject_version=version,
        started_at=started,
        finished_at=datetime.now(UTC),
        results=results,
    )
    _store().append(record)
    print(f"\nrun {record.run_id} recorded")

    if any(r.error for r in results):
        return 2
    return 0 if all(r.passed for r in results) else 1


def cmd_template_drift(_: argparse.Namespace) -> int:
    """Has the template changed without its version comment being bumped?

    The template's own header says "Bump the version on any change", which is a
    promise kept by hand and therefore the one most likely to be broken. The run
    record hashes the template, so a stale version comment means two different
    prompts recorded under one version — and that is precisely the attribution
    the run record exists to provide.
    """
    from narrative import drift_digest

    text = drift_digest.load_prompt()
    match = _VERSION_COMMENT.search(text.splitlines()[0] if text else "")
    declared = match.group(1) if match else None
    print(f"declared version : {declared or 'NONE FOUND in the first line'}")
    print(f"content hash     : {subjects._template_version()}")
    if declared is None:
        print("\nThe template has no version comment to bump.", file=sys.stderr)
        return 1
    print(
        "\nThe hash is what the run record stores. If the template changed and the "
        f"declared version is still {declared}, two prompts share one version number "
        "and a score moved for a reason the record cannot show you."
    )
    return 0


def cmd_degrade(args: argparse.Namespace) -> int:
    """Remove one hard rule from the prompt, rerun, and see what goes red.

    Billed — one call per degradation per case. See `evals/degrade.py` for how
    to read a NO SIGNAL result, which is a statement about the check rather
    than a straightforward pass.
    """
    from unittest.mock import patch

    from config import settings
    from evals import degrade

    key = settings.anthropic_api_key
    if not key:
        print("ANTHROPIC_API_KEY is not set (config reads .env)", file=sys.stderr)
        return 2
    import anthropic

    client = anthropic.Anthropic(api_key=key, timeout=60.0, max_retries=3)
    model = args.model
    wanted = degrade.DEGRADATIONS
    if args.only:
        wanted = tuple(d for d in wanted if d.name in args.only)
        if not wanted:
            print(f"no degradation named {args.only!r}", file=sys.stderr)
            return 2
    by_id = {c.id: c for c in subjects.BILLED_CASES}
    calls = sum(len([cid for cid in d.cases if cid in by_id]) for d in wanted)
    print(f"{calls} call(s) against {model} — this spends money.\n")
    print(f"  {'degradation':<20} {'characteristic':<36} verdict")

    worst = []
    for degradation in wanted:
        try:
            prompt = degrade.degraded_prompt(degradation)
        except ValueError as exc:
            print(f"  {degradation.name:<20} {'—':<36} SKIPPED: {exc}")
            worst.append(degradation.name)
            continue
        cases = [by_id[cid] for cid in degradation.cases if cid in by_id]
        with patch.object(subjects.drift_digest, "load_prompt", lambda p=prompt: p):
            results = [subjects.run_billed(case, client, model) for case in cases]
        broke = sum(
            1
            for r in results
            for c in r.characteristics
            if c.name == degradation.expect_breaks and not c.passed
        )
        errored = [r for r in results if r.error]
        if errored:
            verdict = f"ERRORED: {errored[0].error}"
        elif broke:
            verdict = f"detects ({broke}/{len(results)} case(s) failed)"
        else:
            verdict = "NO SIGNAL — passed without the rule"
            worst.append(degradation.name)
        print(f"  {degradation.name:<20} {degradation.expect_breaks:<36} {verdict}")

    print(
        "\n  Passing this earns no gating rights. It shows the checks are awake, not\n"
        "  that the digest is good — see evals/degrade.py."
    )
    if worst:
        print(f"\n  no signal from: {', '.join(worst)}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a subject against the frozen corpus")
    run.add_argument("subject", choices=[subjects.FREE_NAME, subjects.BILLED_NAME])
    run.add_argument("--model", default=None, help="override the model (billed subject only)")
    run.set_defaults(func=cmd_run)

    drift = sub.add_parser("template-drift", help="declared template version vs content hash")
    drift.set_defaults(func=cmd_template_drift)

    deg = sub.add_parser("degrade", help="strip a prompt rule and check the goldens notice")
    deg.add_argument("--model", default=None)
    deg.add_argument("--only", help="run a single degradation by name")
    deg.set_defaults(func=cmd_degrade)

    args = parser.parse_args(argv)
    if getattr(args, "model", None) is None and args.command in ("run", "degrade"):
        from narrative.drift_digest import MODEL

        args.model = MODEL
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
