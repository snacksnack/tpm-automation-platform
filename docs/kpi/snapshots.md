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

## Which run wins a sim-day (RC1-585)

A day can be snapshotted more than once — a re-run after a fix, or a second
scheduled job the morning a tick failed. `kpi/measures.py` folds the series
into one frame per sim-day before any measure reads it, and the rule used to
be simply *the latest run for a day wins*:

```python
by_day[s.sim_day] = s  # the latest run for a day wins
```

For an ordinary re-snapshot that is right: the later look is the better one.
For a **clock-error** run it is exactly backwards. A mid-converge snapshot
reads a world that does not correspond to the date stamped on it — that is the
whole of RC1-417 — so it is the *worst* available view of its day, not the
best.

The measure already knew this about the day being reported: a clock error on
`series[-1]` takes the carry path (RC1-511). What it did not know is that the
same run was then handed to every **later** day whose trailing window starts on
it. `forecast-slip-days` is the one measure with such a window
(`SIM_WINDOW_DAYS = 14`, `then = frames[snap.day - 14]`).

### What it cost

Sim-day 47 sits inside the scripted source break (days 43-47), so the correct
view of it is *no stories under the program*. It has two runs:

| run | clock | jira | stories | Done pts |
|---|---|---|---|---|
| 109 | ok | `missing(0)` | 0 | 0 |
| 111 | **error** | `ok(32)` | 31 | 109 |

Run 111 caught the day-48 label restoration part-way while converging past day
47. "Latest wins" picked it, so day 61 — whose window starts on day 47 — saw 31
stories already Done and counted 37 newly-done points instead of 146:

```
with run 111 (before):  -4.11  | 5 pts remaining at  2.643 pts/day ( 37 pts Done in the last 14 days)
with run 109 (after):   -5.52  | 5 pts remaining at 10.429 pts/day (146 pts Done in the last 14 days)
ledger expects:         -5.52  | 5 pts remaining at 10.429 pts/day (146 pts Done in the last 14 days)
```

Two days were affected, and only one of them was ever reported:

| window start | affected day | stored | ledger | gap | flagged by the ledger check |
|---|---|---|---|---|---|
| 47 | 61 | -4.11 | -5.52 | 1.41 | yes |
| 52 | 66 | 0.27 | 0.04 | 0.23 | **no — tolerance is 1** |
| 66 | 80 | — | — | — | beyond `last_day` 69 |

Day 52's own reading is correct only by accident: it was repaired by hand on
2026-11-02 from run 122, the clock-ok run, which is this fix applied manually.

**The lesson worth keeping is the last column.** The ledger check compares each
reading against its row's `tolerance`, so a divergence smaller than the
tolerance is invisible. "N divergent" is a floor, not a count. When chasing a
measure bug, compare *exactly* and treat the tolerance as the pass criterion
only at the end.

### The rule now

Prefer a clock-ok run over a clock-error run for the same sim-day; keep the
error run only when it is the only run that day, because a missing day is a
gap and every measure reads `broken` on a gap. The day's own clock-error branch
reads `series[-1]` and is untouched, so RC1-417 and RC1-511 behave exactly as
before.
