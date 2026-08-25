# Narrate — the weekly SVP brief (RC1-306)

The fifth stage. Track lands the numbers every morning; once a week this
writes the prose — trend, what moved, what it means, asks — archives it
beside the readings, and posts it to Slack.

Claude writes the narrative; the numbers come from the track stage
unchanged, and that is enforced rather than promised. `build_payload` is
deterministic Python that hands the model every number it is allowed to
say — each shipping KPI's latest reading, its value a week ago, the delta,
a 28-day series, and flags for what newly tripped, broke or recovered —
and `audit_numbers` refuses any brief whose prose contains a number the
payload cannot vouch for. A rejected brief exits 2, is never archived and
never posted. The brief's number lines (value, delta, state marker) are
rendered by code; the model's sentences sit beside them, never in place of
them.

- Code: `kpi/narrate.py` (the stage), `kpi/briefs_store.py` (the archive),
  `kpi/templates/narrate.md` (the versioned prompt),
  `scripts/kpi_weekly.sh` + `scripts/launchd/com.reidcollins.kpi-weekly.plist`
  (Monday 08:00 local, an hour after the daily job).
- Transport: the same incoming webhook as the drift digest
  (`drift/notify.py`, `SLACK_WEBHOOK_URL` in `.env`).

```bash
python -m kpi.narrate --program eval-run-store --no-archive   # write and print only
python -m kpi.narrate --program eval-run-store                # write and archive
python -m kpi.narrate --program simulated-program --post      # archive and post to Slack
scripts/kpi_weekly.sh --dry                                   # both programs, no posting
```

Exit codes: **0** the brief was written (and posted, with `--post`); **2**
it could not be — no `EVAL_DATABASE_URL`, no `ANTHROPIC_API_KEY`, nothing
tracked yet, or a brief the numbers audit refused. There is no exit 1: a
half-brief is not a deliverable.

## What the sponsor sees

One screen, in this order, every week:

1. **Headline** — at most two sentences, outcomes only.
2. **Outcomes** — one line per outcome KPI: state marker (🟢 ok, 🟡 stale,
   🔴 tripped or broken), the value and week-over-week delta rendered by
   code, the model's one-sentence read, and any `[stale: …]` / `[broken: …]`
   / `[proxy: …]` label. Leading and activity metrics never appear here.
3. **What moved** — the leading indicators as explanation, using the tree's
   `leads` mechanisms; this is the only place they may carry numbers.
4. **Asks** — up to three, each a decision, omitted entirely when there are
   none.
5. The gaps (`Not in this brief: …` for tree KPIs that do not ship) and the
   trace line naming the snapshot `run_id` and sim-date.

## Tracing a number back to its snapshot

Every brief row in `kpi_briefs` keeps the exact `payload` the model saw,
the `narrative` it returned, the rendered `brief`, and the `run_id` of the
snapshot the readings were computed from. The chain a reviewer walks:

    kpi_briefs.payload            -- the number, with its kpi_id and sim_date
    kpi_readings                  -- the same reading, landed by the track stage
    python -m collectors show <program> --run <run_id>   -- the snapshot it was computed from

`posted_at` is null until Slack accepted the post, and a re-narrated week
keeps its original `posted_at` (the UPSERT coalesces) — the archive records
what was sent, not what was drafted last.

## The done-when clock

Three consecutive weekly briefs for the real program (`eval-run-store`)
must post — with the Monday schedule that is three calendar weeks from the
first posted brief, and no amount of code finishes it sooner. The
simulated program's brief rides the same schedule; its planted events
(cost spike at sim-day ~42, source break at 43–47) will arrive with the
sim clock and must be called out by the brief of the week they land.
