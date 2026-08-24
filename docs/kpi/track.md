# Track — computing the readings (RC1-305)

The fourth stage. Define drafted the tree, instrument proved which KPIs
actually compute, and this runs them every morning and lands the numbers
where a dashboard can read them.

Nothing in this stage calls a model. The measures are ordinary Python over
the snapshot series, which is the epic's "deterministic numbers, LLM
narrative" line drawn in code: RC1-306 will write prose *about* these
readings and will not be allowed to compute one.

- Code: `kpi/track.py` (the stage), `kpi/readings_store.py` (Postgres),
  `kpi/measures.py` (the computations, RC1-303),
  `kpi/dashboards.py` (the Grafana JSON).
- The reading model: `kpi/reading.py` — the same shape the ground-truth
  ledger uses, so the eval suite's diff is a comparison rather than an
  adapter.

```bash
python -m kpi.track --program simulated-program            # compute and store
python -m kpi.track --program eval-run-store --dry-run     # compute and print, write nothing
python -m kpi.track --program simulated-program --sim-date 2026-09-08   # re-read one day
python -m kpi.dashboards --out grafana/                    # regenerate the dashboards
```

Exit codes mirror `python -m collectors snapshot`: **0** every KPI read ok,
**1** at least one is stale or broken, **2** the stage could not run at all
(no snapshots, no instrument report, no `EVAL_DATABASE_URL`). The readings
are stored on a `1` — a day a KPI could not be measured is exactly the day
worth recording.

## What ships

The instrument stage's verdict decides, not this stage:
`docs/kpi/instruments/<program>.json` lists every KPI it verified, and only
those are tracked. A rejected KPI has no source, a proxied-but-unverified one
has nothing that computes it, and neither gets to appear on a dashboard
looking like a number somebody stands behind.

Re-instrument and the tracked set changes with it. Regenerate the dashboards
in the same commit — a test fails if the committed JSON no longer matches the
generator.

## Where the readings land

Postgres on `reid-eval-store`, decided in RC1-304 —
[`metrics-store.md`](metrics-store.md) has the argument. One row per program,
KPI and sim-date:

| column | what |
| --- | --- |
| `program_id`, `kpi_id`, `sim_date` | the primary key; a re-run of a day replaces it, last word wins |
| `value` | the number, or `NULL` when there isn't one — never `0` for unknown |
| `state` | `ok`, `stale` or `broken` |
| `tripped` | the KPI's so-what threshold is crossed on this reading |
| `as_of` | the date of the data behind `value`, earlier than `sim_date` when carried |
| `reason` | why the state is not ok; the table refuses a non-ok row without one |
| `detail` | the working — inputs, chain, halves |
| `run_id` | the snapshot the number came from |
| `computed_at` | wall clock |

**Staleness is a state, never a zero.** `kpi/reading.py` validates it,
`kpi_readings` constrains it (`state = 'ok' OR reason IS NOT NULL`), and the
charts draw a null as a gap rather than joining across it. Three layers
because the failure is silent: a zero on a dashboard looks like a
measurement, and nobody asks a question about it.

**Every number traces back.** `run_id` names the snapshot the reading was
computed from:

```bash
python -m collectors show simulated-program --run 7
```

It is a reference, not a foreign key — the snapshots are SQLite and the
readings are Postgres, so the id is only meaningful against this machine's
`drift.db`. That holds while the launchd job is the single writer, which it
is. A second writer means moving the snapshot store here too.

## The daily run

`scripts/kpi_daily.sh` does tick → snapshot → track, in that order, at 07:00
local ([`simulator.md`](simulator.md) has the launchd details). Order is why
it is one job: converge the world, record it, then read it. Tracking against
yesterday's snapshot would date every reading a day behind the program.

A `1` from the job is routine rather than an alarm. The simulated program's
spend line has no landed week until day 7, so `cost-vs-envelope` and
`weekly-spend-burn-ratio` read stale and the job exits 1 every morning until
then. `launchctl list` shows that as the last exit status. Read
`data/kpi-sim/daily.log` before assuming something broke.

## The dashboards

Generated from the trees and the instrument reports into `grafana/`:
one per program plus a cross-program view. They are generated rather than
drawn because a hand-made dashboard goes stale the moment the instrument
stage changes its mind, and a stale dashboard is exactly the failure this
epic is about.

| file | what |
| --- | --- |
| `grafana/simulated-program.json` | the simulated program; the Cost panel is where the planted week-6 spike shows |
| `grafana/eval-run-store.json` | the real program: pass rate, cost per case, freshness |
| `grafana/portfolio.json` | both programs' latest readings, and how many KPIs each could measure per day |

Each carries an `__inputs` datasource placeholder, so importing prompts for
the Postgres connection instead of dragging a uid from another instance.

**Importing (one-time, needs a Grafana Cloud account):**

1. Sign up at grafana.com — the forever-free tier is three users, which is
   enough. No card.
2. **Connections → Data sources → Add → PostgreSQL.** Host, database, user
   and password come from `EVAL_DATABASE_URL` in `~/.zshrc`; TLS/SSL mode
   **require**. Save and test.
3. **Dashboards → New → Import → Upload JSON file**, one per file above, and
   pick that Postgres datasource when prompted.

The free tier's 14-day metric retention does not apply here: that limit is on
Grafana's own metrics store, and these panels query Postgres directly, where
the rows stay as long as we keep them.
