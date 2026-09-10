# Alerting on the crossing, not the level (RC1-418)

`Program KPI tripped — <program>` used to query the standing state:

```
max(last_1d):max:kpi.program.tripped{program:X} by {kpi} > 0
```

That works only for a KPI that can come back. Some cannot.

## Latching KPIs

`scope-change-pct` is `(added − removed) ÷ baseline × 100` against the points
visible on **day 0**. The baseline never moves, and the scenario never removes
the scope it adds, so once the reading crosses it stays crossed. Two of the
simulated program's six KPIs are this shape:

| KPI | first trips | tripped for | recovers |
| --- | --- | --- | --- |
| `scope-change-pct` | day 17 | 53 days | never |
| `cost-vs-envelope` | day 49 | 21 days | never |
| `forecast-slip-days` | day 3 | 19 days | yes, oscillates |
| `critical-path-slack-days` | day 29 | 12 days | yes, oscillates |
| `weekly-spend-burn-ratio` | day 42 | 7 days | yes, oscillates |
| `blocked-share-pct` | never | 0 days | — |

(From `docs/kpi/ledger/simulated-program.csv`, the ground-truth ledger.)

This landed for real on 2026-09-10. Sim-day 17, the scripted `scope-add`
event: `scope-change-pct` read **11.85 %** against a 10 % threshold — matching
the ledger to the digit — and monitor 317618032's `kpi:scope-change-pct` group
went to Alert. On the old query it would have stayed there for the program's
remaining 52 days.

## Why that is worse than noisy

The monitor is the SLI for **Program health** (95 % / 30d, monitor-based —
"fraction of time the monitor sat in OK"). A group that can never return to OK
holds the error budget at zero indefinitely, and no amount of target
calibration fixes it, because the input is permanently 1. RC1-409 calibrated
this SLO once already; this is the same tension with a KPI that has no
recovery path.

A permanent red also stops being a signal. The incident was the day scope
crossed. Every day after is a status, and a status belongs on a dashboard.

## What changed

A second gauge ships beside the level:

| metric | meaning | read by |
| --- | --- | --- |
| `kpi.program.tripped` | the standing level, 0/1 | the dashboard's "tripped thresholds" widget |
| `kpi.program.newly_tripped` | 1 only on the day the KPI crossed | the monitor |

`newly_tripped` is 1 when a KPI is tripped today and was not tripped in the
**previous stored reading** — what actually went out yesterday, not a
recomputation, because the question the monitor asks is "have we already
raised this?". `kpi/track.py` reads that day from the readings store *before*
it writes today's rows, so a re-track of the same day does not read its own
output back as yesterday and re-fire every standing trip.

`kpi/datadog.py` stays pure: `newly_tripped()`, `series_for()` and
`ship_readings()` all take `previous` as an argument and touch no store.

Three edges, each with a test:

- **No previous day at all** → a tripped KPI is newly tripped. The first
  observation is the news.
- **A KPI that stays tripped** → 0. The latching case, and the point of this.
- **A `broken` reading carrying tripped through a source break** (days 43–47)
  → 0. It was tripped then and is tripped now; carrying is not crossing.

The 0 ships every day rather than only on trip days: a metric that appeared
only when something crossed would leave the alert with no datapoint to
resolve against.

## What did not change

- `kpi.program.tripped` ships exactly as before, and the dashboard reads it.
- The monitor keeps its name. `push_monitors` matches by name, so the push
  updates monitor 317618032 in place and the `Program health` SLO's
  `monitor_ids` wiring survives. A test pins both.
- The reading events (RC1-329) are untouched — they already rolled a standing
  trip up under one `aggregation_key` per (program, kpi), which is the same
  instinct this applies to the metric side.

## Deploying it

```bash
python -m kpi.datadog monitors --push     # updates 317618032 in place
```

The generated monitors are deliberately outside `datadog/manifest.json` (see
`docs/datadog-as-code.md`), so this does not touch the daily drift check.

The standing `kpi:scope-change-pct` alert clears on the first daily run after
the push, when `newly_tripped` ships its 0 for the day.
