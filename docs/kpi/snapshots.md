# Program snapshots — the collector (RC1-301)

KPIs are computed from dated snapshots, never from Jira's changelog. Jira
cannot backdate a transition, so a simulated program's history exists only
as the sequence of days someone looked — and a real program's history is
the same thing, just with the wall clock doing the looking. One snapshot per
run per program; a day's readings come from that day's snapshot.

- Code: `collectors/program.py` (`collect_program`), registry
  `collectors/programs.py`, store `store/snapshot_store.py`, CLI
  `python -m collectors`.
- The snapshot model: `collectors.models.ProgramSnapshot`.

```bash
python -m collectors snapshot simulated-program    # collect every source, store, print health
python -m collectors runs simulated-program        # every stored run: sim-day, sim-date, health
python -m collectors show simulated-program        # the latest; --sim-date D, --run N, --json
```

## What a snapshot holds

| section | source | for |
| --- | --- | --- |
| `jira` | the program's issues by label — status, dates, points, labels, links, type, parent; the epic included for the committed GA date | the four Jira-sourced KPIs |
| `spend` | the simulator's weekly line, only the weeks that have landed (`data/kpi-sim/spend.csv`) — a real billing feed is RC1-308 | `cost-vs-envelope`, `weekly-spend-burn-ratio` |
| `eval_runs` | one row per run in the shared eval store: subject, version, cases / passed / errored, cost — counts, not the record | the eval-run-store program's tree |
| `health` | one row per source: `ok` / `missing` / `error`, a count, a reason | the escalate stage (RC1-307) |

Two stamps on every snapshot. `collected_at` is the wall clock.
`sim_date` (and `sim_day`) come from the simulator's `clock.json` when the
program has one; for a program without a clock, `sim_date` is today. The
KPI stage computes against `sim_date` and never needs to know which kind
of program it has.

## Missing, never zero

The collector never raises for a source problem and never leaves a section
looking measured when it was not:

- **`error`** — the source could not be read (no credentials, HTTP 503, a
  driver missing). Its section is *absent*: `jira` is `None`, not `[]`.
- **`missing`** — the source answered with nothing: the Jira query returned
  no issues, the spend file has no landed weeks, `eval_runs` is empty. The
  section is present and empty, and the health row says so.
- **`ok`** — with a count.

The simulated program's week-7 source break arrives here as
`jira missing, 0 issues` while `spend` stays `ok` — which is exactly the
shape the KPI tree asks for: the Jira KPIs go *broken*, the cost KPIs keep
reporting, and nothing reads 0 % scope change. `snapshot` exits 1 when any
source is not `ok`, and **stores the snapshot anyway**: "the source was gone
on this day" is a fact the program needs kept, and a run that refused to
record it would leave a gap indistinguishable from "nobody looked".

## One store

Snapshots land in the drift detector's SQLite store (`DB_PATH`,
`data/drift.db`) — the same `runs` table, widened: a run may carry a
`program_id`, `sim_date` and `sim_day`; issue rows gained type, labels,
points, created and parent; three tables sit beside them for spend, eval
runs and health. An existing database is migrated in place on open (new
nullable columns, nothing rewritten; the append-only rule is untouched).

This is the RC1-233 Portfolio Console constraint — *one snapshot per run*
— honoured the only way it can be: there is one place runs live, and the
console and the KPI agent read the same rows.

## Programs

`collectors/programs.py` registers the two programs the KPI agent has
trees for, keyed on the brief's filename in `docs/kpi/programs/`:

| id | Jira | spend | clock | eval store |
| --- | --- | --- | --- | --- |
| `simulated-program` | PMA, `labels = "kpi-sim"` | `data/kpi-sim/spend.csv` | `data/kpi-sim/clock.json` | — |
| `eval-run-store` | — | — | wall clock | `EVAL_DATABASE_URL` |

The eval store's DSN is read from the process environment, never from a
repo `.env` (RC1-263); by hand that means a shell with `~/.zshrc` loaded,
and the daily run reads the line from the profile itself.

## The daily snapshot

Both programs are snapshotted every morning by the KPI daily run —
`scripts/kpi_daily.sh`, launchd at 07:00, tick first and then
`snapshot simulated-program` and `snapshot eval-run-store`. The runbook
for installing and watching it is [`simulator.md`](simulator.md#the-daily-run).
`EVAL_DATABASE_URL` needs no setup: the script reads the one `export` line
from `~/.zshrc`, its only home.

Jumping with `python -m simulate to-day N` and then
`scripts/kpi_daily.sh --no-tick` records any day on demand; a sim-date
snapshotted twice keeps both runs, and `show --sim-date` / a recompute use
the latest.

## Recomputing a day, offline — the done-when

`simulate.ledger.snapshot_from_collected` turns a stored `ProgramSnapshot`
into the ledger's snapshot shape (stories keyed by their `ks-` slug, the
epic's due date as the GA day, spend rows as landed), and
`ledger.derive(series=[...])` runs the adopted tree's formulas over a
collected series instead of the scenario. The test
`test_a_days_kpis_recompute_from_collected_snapshots_alone` does the whole
chain with no network: the simulator converges an in-memory Jira for days
0–69, the collector parses it with the same parser that reads live Jira,
the store round-trips every day, and the recomputed ledger equals the
scenario-derived one reading for reading — value, state, tripped, as-of.

That is the seam the track stage (RC1-305) sits on: its measures read
collected `ProgramSnapshot`s, never the scenario — and since RC1-310 they
compute the simulated program's KPIs themselves (`kpi/measures.py`), with
the ledger kept in `simulate/` as the independently-written expectation
the `kpi-ledger` eval diffs them against.
