# Ground-truth ledger — Observability Platform GA (simulated)

What every KPI *should* read on every sim-day, derived from the scenario
(RC1-300). The simulator converges Jira to `scenario.state_at(day)`; the
ledger applies the [adopted tree's](trees/simulated-program.review.md#adopted-tree-what-rc1-303-instruments)
formulas to the same function. The two cannot disagree about what day N
looks like, which is what makes the ledger a ground truth rather than a
second opinion.

- Code: `simulate/ledger.py` (`derive()`), contract: `kpi/reading.py`
- The ledger itself: [`ledger/simulated-program.csv`](ledger/simulated-program.csv),
  420 rows (70 days × 6 KPIs). Regenerate with
  `python -m simulate ledger --out docs/kpi/ledger/simulated-program.csv`;
  a test fails if the committed copy is stale. The simulator also writes it
  to `data/kpi-sim/ledger.csv` on every converge.
- The suite: `python -m evals run kpi-ledger` — `evals/kpi_ledger.py`.

## The reading

One shape on both sides of the diff. The track stage (RC1-305) emits a
`Reading` per KPI per snapshot; the ledger is a `Reading` per KPI per day
plus a tolerance.

| field | meaning |
| --- | --- |
| `kpi_id`, `sim_date` | which KPI, and the day the reading is *for* |
| `value` | the number, or `None` — never 0 for "unknown" |
| `state` | `ok` · `stale` (older than `stale_after`, or no value has ever existed) · `broken` (the source is gone; the value is carried from the last good day) |
| `tripped` | the KPI's so-what threshold is crossed on this reading |
| `as_of` | the date of the data behind the value — earlier than `sim_date` when carried |
| `reason` | required whenever the state is not `ok` |
| `detail` | the working: remaining points and rate, the link that set the minimum, the two halves of blocked share |

A reading that is not `ok` without a reason does not validate. That is the
rubric's honesty rule as a type: the instrument is allowed to say "I don't
know", and it has to say why.

## Derivation, per KPI

The collector's view is reconstructed from the scenario: only issues carrying
the program label (`kpi-sim`) are visible, spend rows appear the Monday after
their week, and the committed GA date is the epic's due date. Today's and
yesterday's snapshots are both available, as they will be to the track stage.

| KPI | formula (adopted tree) | tolerance | trips when |
| --- | --- | --- | --- |
| `forecast-slip-days` | `today + remaining ÷ rate − GA`. `rate` is points moved to Done over the trailing **14** days ÷ 14 (diffing the two snapshots); before day 14, the plan rate `total ÷ 70`. Zero rate → `broken`, "no completed work to forecast from". Remaining = 0 → the first day of the current all-Done stretch, minus GA. | 1 day | > +5 |
| `cost-vs-envelope` | cumulative actual − cumulative plan through the last landed week, USD; `%` of plan-to-date in the detail. | $1 | cumulative > 110 % for two consecutive landed weeks |
| `scope-change-pct` | `(added − removed) ÷ baseline × 100`, baseline = points visible on day 0, added = points on stories not in the day-0 snapshot, removed = day-0 stories no longer visible. | 0.5 pt | net > +10 % |
| `critical-path-slack-days` | over every `blocks` link whose downstream is on a chain to a `ga-blocking` story: `downstream.start − upstream.due`; the minimum, with the link named. | 0.5 day | < 3 days |
| `blocked-share-pct` | points on open GA-chain stories that are Blocked (direct) or have an open upstream that is Blocked or past due (transitive), ÷ **all** open points. | 1 pt | > 25 % for three consecutive days |
| `weekly-spend-burn-ratio` | latest landed week's actual ÷ plan. | 0.01 | > 1.5, or > 1.2 for two consecutive weeks |

**Source break.** The detection rule from the tree: the count of issues under
the program label falls below half of the last good day's. While it holds,
every Jira-sourced KPI reads `broken`, carrying the last good reading —
value, tripped and `as_of` — with a reason naming the counts and the day.
The cost KPIs are a different source and keep reporting. Day 48 is `ok`
again, recomputed fresh.

### Decisions the tree left open

Made here; argue with them in review, and the ledger changes on the next
derive.

1. **Done links drop out of the slack minimum.** A delivered upstream cannot
   consume slack, and a delivered downstream has nothing to protect. Without
   this, the −11 from the week-5 slip would read forever. With it, slack
   returns to +3 on day 41 when `t-context` lands, climbs to +7 as the chain
   completes, and reads *no value* ("no open dependencies on a GA chain")
   from day 59 when every link into the sign-off is delivered.
2. **A late upstream with no re-planned date under-reports.** The formula
   uses the due date as written; an upstream past its due and not Done is
   caught by the *transitive* half of blocked share, not by slack. Known,
   and the same choice the drift detector's timeline-inversion rule makes.
3. **Blocked share: GA-chain numerator, all-open denominator.** The review
   restricted the indicator to GA-blocking chains; the share is of the
   program's open work, because that is the denominator the sponsor holds in
   their head. Off-chain blockage (`p-security`, days 27–33) does not count.
4. **"No value yet" is `stale`.** Before the first spend row lands (days 0–6)
   the cost KPIs have never had a value; the honest age of the freshest value
   is infinite. `broken` is reserved for a source that *was* there.
5. **The GA root is a label.** A snapshot has no `ga_blocking` flag, so the
   simulator labels the sign-off story `ga-blocking` and the chain is
   computed from the snapshot's links. That is how the track stage will find
   it on a real program: the TPM marks the root.

## The planted events, as the ledger reads them

Each event names its `must_move` KPIs and, among them, the **first-day
detector** (rubric amendment 3) that must show the signal by the day after.
`ledger.reactions()` measures when each KPI first moves against its reading
on the day before the event; a test holds the detectors to ≤ 1 day and the
rest to ≤ 14.

| event | day | detector | signal | also moves |
| --- | --- | --- | --- | --- |
| `scope-add` | 16–17 | `scope-change-pct` | 9.63 % on day 16, **11.85 % tripped on day 17** | forecast, same day (remaining points) |
| `upstream-slip` | 29 | `critical-path-slack-days` | **+3 → −11, tripped, day 29** (`t-context` due → `s-latency` start) | blocked share 0 → 18.7 % on day 30, 30.9 % by day 41; forecast |
| `cost-spike` | 42 | `weekly-spend-burn-ratio` | **2.04, tripped, day 42** | `cost-vs-envelope` +170 → +1,420 the same day; trips on day 49 (two weeks over 110 %) |
| `source-break` | 43–47 | the four Jira KPIs | **`broken` on day 43**, carrying day 42 | cost KPIs unaffected |

Two observations the derivation surfaced, recorded for the rubric's v2 rather
than smoothed away:

- **The forecast is noisy by construction.** A 14-day window on a five-person
  team swings the rate between 1.4 and 3.6 points/day; the forecast ranges
  −9 to +35 days and trips on 19 of 70 days, most of them before the slip.
  The tree's own failure-mode list predicted this ("denominator collapse",
  "one large story closing swings it"). The ledger encodes the adopted
  definition faithfully; whether the so-what threshold should be held for
  N consecutive days, as blocked share's is, is a v2 question.
- **Blocked share rises but never trips.** It climbs 0 → 30.9 % through weeks
  5–6 and holds above 25 % on days 40–41 only — the three-consecutive-day
  rule is not met. The indicator did what the tree says (it *rises* in weeks
  5–6); the threshold did not fire. Also a v2 question.

A compact view — `?` stale, `!` broken, `*` tripped:

```
day  date        forecast-slip-  cost-vs-envelo  scope-change-p  critical-path-  blocked-share-  weekly-spend-b
  0  2026-09-07               3              -?               0               3               0              -?
  7  2026-09-14            4.81             -50               0               3               0          0.9583
 14  2026-09-21           -6.03             -10               0               3               0          1.0333
 16  2026-09-23            1.84             -10            9.63               3               0          1.0333
 17  2026-09-24           -5.28             -10          11.85*               3               0          1.0333
 28  2026-10-05          35.42*              90          11.85*               3           16.83          1.0917
 29  2026-10-06           5.93*              90          11.85*            -11*               0          1.0917
 30  2026-10-07           6.93*              90          11.85*            -11*           18.68          1.0917
 41  2026-10-18           -9.26             170          11.85*               3           30.91          1.0667
 42  2026-10-19           -8.26            1420          11.85*               3           21.82         2.0417*
 43  2026-10-20          -8.26!            1420         11.85!*              3!          21.82!         2.0417*
 47  2026-10-24          -8.26!            1420         11.85!*              3!          21.82!         2.0417*
 48  2026-10-25           -8.84            1420          11.85*               3           18.92         2.0417*
 49  2026-10-26           -9.22           1610*          11.85*               3           21.88          1.1583
 59  2026-11-05           -7.22           1730*          11.85*               -               0             1.1
 66  2026-11-12            0.04           1800*          11.85*               -               0          1.0583
 67  2026-11-13               0           1800*          11.85*               -               -          1.0583
```

`python -m simulate ledger` prints all seventy days; `--day N` shows one day
with the working.

## The suite

`kpi-ledger` is the third subject in `evals/`, free and deterministic, so it
gates CI beside `drift-digest-allclear`. Seventy cases, `day-00` … `day-69`,
tagged by week and by the events active that day. Each case scores an
implementation's readings for the day:

- one characteristic per KPI, named after it — value within tolerance (or
  both sides have no value), the same state, the same tripped;
- on the day after each planted event, `detects-<event>`: the detector reads
  the signal. A subset of the per-KPI check by construction, named separately
  because "the number was right and nobody noticed" is its own failure.

The implementation is pluggable, and since RC1-310 the reference is `track`:
the track stage's own measures (`kpi/measures.py`) over collected-shaped
snapshots rendered from the scenario (`simulate/collected.py`). The two sides
are written separately — the ledger derives in `simulate/`, the measures read
`ProgramSnapshot` in `kpi/` and import nothing from the simulator — so the
recorded run is a real check, not the tautology it was before the formulas
moved out of `simulate/`:

```bash
python -m evals run kpi-ledger                             # track vs ledger: 70/70, recorded
python -m evals run kpi-ledger --impl no-break-detection   # 65/70: days 43-47 fail, exit 1
python -m evals run kpi-ledger --impl window-28            # 29/70: the forecast differs
```

`no-break-detection` trusts an empty snapshot: on day 43 it reads 0 points
remaining (forecast −24), −100 % scope, no links, state `ok` on all four —
the zero-for-unknown failure the rubric exists to prevent, and
`detects-source-break` names it on day 44. `window-28` is the agent draft's
window the review rejected; it passes the plan-rate days and the break, and
fails wherever the two windows disagree. Neither run is recorded: the store
holds measurements, not demonstrations.

The ledger itself did not change when the track stage took over as the
reference: it stays the independently-written expectation in `simulate/`,
and a disagreement between the two is either a formula bug or an ambiguity
in the tree's definition — both worth finding before a weekly brief
(RC1-306, RC1-308) depends on the numbers.
