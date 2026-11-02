# A converge that fails part-way (RC1-417)

On 2026-09-08 the daily job's tick applied 11 of sim-day 16's actions to Jira —
created the three mobile-BFF stories, set points, added links — and then raised
`httpcore.ReadTimeout` on the last `set_estimation`. `_converge_to` writes the
clock only *after* `apply.converge` returns, so the exception skipped it:

- Jira had advanced to day 16.
- `clock.json` still read day 15.
- Three minutes later the snapshot stamped day 16's world as **day 15**, and
  `kpi.track` wrote those numbers to the `2026-09-22` rows.

Two readings were wrong for two days (`scope-change-pct` 9.63 where the ledger
says 0; `forecast-slip-days` 0.84 where it says −5.03) before anyone looked.
Days 16 and 17 self-corrected, because converge is idempotent.

## Why nothing went red

`escalate` exits 1 every morning on the standing flatline finding, so
`done exit 1` is the daily norm. A tick that half-applied the world produced the
same exit code as a healthy run. `simulate verify` — which compares Jira against
the scenario at the clock's day and would have caught it that morning — existed
and was not in the daily job.

## The shape of the fix

There is no honest day number for a world half-way between two days, so the
clock does not invent one. It records that it does not know.

**1. The failure is a distinct kind of failure.** `apply.converge` raises
`ConvergeError` carrying the day it was reaching for, the actions that landed,
and the keys created so far. Each action goes through `_apply`, so there is one
place to catch it.

**2. The clock records a dirty world instead of a silent one.**
`SimState.mark_incomplete(converging_to, keys)` leaves `day` on the last day
that *fully landed* and writes `converged: false` beside it. The spend line and
the ledger are not rewritten — they are functions of a day the world never
reached. The manifest is, so keys a partial converge created are not lost.

**3. The exit code says which failure it was.** `python -m simulate` now exits
**3** for a dirty world, distinct from the **1** that means a tick was refused.
That distinction matters: the program ends on day 69 and every tick after it is
refused, so 1 is routine and 3 never is. `tick` also stops on a dirty converge
rather than walking to the next day and stacking a second half-applied day on
the first.

**4. The collector reports it as an error, and still records the day.** A clock
with `converged: false` makes the `clock` source `error` — never `missing`,
never silently `ok`. The snapshot is taken and stored, because "the day the
simulator was mid-converge" is a fact the KPI stage needs; `snapshot` exits 1.

**5. The measures refuse to date a number from it.** Every simulated KPI reads
`broken` with a reason naming the day converge was reaching for. This is the
honesty rule — *never a number computed from an absence* — with the absence
being the **date** rather than the data. The `broken` reading carries the last
good day's value *and* its `tripped` flag through, re-stamped with the dirty
date — it does not invent a number, and it does not drop the standing trip
(RC1-511, below).

**6. `simulate verify` runs in the daily job**, after the tick and before the
snapshot. It reads Jira, writes nothing, and goes red the morning a clock and a
world disagree for any reason, including ones this design has not thought of.

## Recovery is the next tick

Nothing needs a human. The clock still points at the last good day, so the next
tick advances to the day that failed and re-converges it; converge is
idempotent, so it repairs whatever half-landed and writes `converged: true`.
That is exactly what happened by hand on 09-09 and 09-10.

## What this does not do

It does not make a partial converge atomic. Jira has no transaction across
issues, and a rollback would be a second batch of writes that can fail the same
way. The design accepts a half-applied world and refuses to *date* it, which is
the part that corrupted data.

## Repairing a day that was already mis-dated

`kpi.track --sim-date D` will not do it: `latest_program_run(sim_date=...)`
takes the *last* run for a day, which is the corrupted one. Recompute from the
good run with the series truncated there and write through
`ReadingsStore.save(..., run_id=<good run>)`. Verify by comparing every stored
reading against `docs/kpi/ledger/simulated-program.csv` — the whole ledger, not
just the day touched.

## A dirty day must not reset the crossing (RC1-511)

On 2026-10-18 the Program KPI monitors fired two crossing alerts for
`simulated-program` — `scope-change-pct` and `cost-vs-envelope` — with nothing
in the program having changed. Both had been standing-tripped for weeks (the
scripted scope add and the week-6 cost spike). The crossings were spurious.

`kpi.program.newly_tripped` (RC1-418) fires on the day a KPI goes from not-tripped
to tripped against the last reported day. The mid-converge guard returned a bare
`_broken(...)`, which defaults `tripped` to `False`. So a dirty day dropped the
standing trip, and the next clean run read as a fresh crossing and re-alerted:

- run 124 (sim-day 52, `clock=error`): both KPIs `broken, tripped=False`.
- run 126 (sim-day 53, clean): both `ok, tripped=True` → `newly_tripped=1` → two alerts.

The Jira source break never had this bug — it recomputes the last good day and
re-stamps it `broken`, keeping value *and* `tripped`. The fix makes the
clock-error branch do the same: recompute as of the prior snapshot and carry its
standing state through. The monitors were correct; the measure was resetting
their baseline. Any mid-converge blip would otherwise re-fire every still-tripped
KPI the following morning.

## A timeout is not a dirty world (RC1-584)

On 2026-11-01 the guard above cost a full day of KPI measurement for a world
that was never dirty.

`launchd` started the 07:00 job inside a **2-second DarkWake** — the laptop was
asleep on battery — and the machine went back to sleep one second later:

```
2026-11-01 07:07:28  DarkWake from Deep Idle ... Using BATT (Charge:100%) 2 secs
2026-11-01 07:07:30  Entering Sleep state 'Sleep Service Back to Sleep'
```

Every network call then timed out. The run limped across later DarkWakes until
07:24, and Heroku Postgres dropped in the middle of it too
(`psycopg2.InterfaceError: connection already closed`, in `escalate`).

`data/kpi-sim/daily.err`:

```
day 67 (2026-11-13): CONVERGE FAILED after 0 action(s) ... Cause: The read operation timed out
```

Day 67 needed exactly one action, `status p-ga -> Done`. **It landed.** Only the
response confirming it was lost, so `ConvergeError` was raised with `applied`
empty, the clock was marked incomplete, and all six sim KPIs shipped `broken`.
Jira was by then exactly at day 67 — which the re-converge on 11-02 proved, by
finding it in sync with 0 actions and `verify` reporting "day 67: Jira matches
the scenario".

The bill for one timed-out read: six `broken` readings, six escalations posted
to Slack, run 156 overwriting day 66's good rows on the same sim-date, and the
*Program KPI unmeasured* monitor firing on `cost-vs-envelope` the next morning,
once the preceding `ok` point aged out of `min(last_2d)` (RC1-506).

### Why the fix is a retry, not a shortcut

`applied` excludes the action that raised. So `0 action(s)` means **the first
action was unconfirmed, not that Jira is untouched** — as here, it may well have
landed. Skipping `mark_incomplete` when `applied` is empty would therefore
reintroduce exactly the RC1-417 bug the first time a first action lands and a
second one does not.

Only a clean converge proves what day the world is on, and converge is
idempotent. So `_converge_to` retries once, after `RETRY_PAUSE_S`, before
reaching the dirty verdict. A transient timeout now costs 20 seconds instead of
a day; a genuinely half-applied world still fails twice and is still recorded
unreadable. The guard is unchanged in substance — it just stops being triggered
by the network.

### And the cause underneath

`scripts/kpi_daily.sh` re-execs itself under `caffeinate -i`, so the job holds
the machine awake for its duration instead of dying in a two-second wake. This
is the same remedy that recovered the 2026-10-05 incident by hand.

`caffeinate -i` does **not** wake a sleeping machine. A run that never starts is
still a missed run, and the heartbeat monitor is what covers that.

### This was the fourth occurrence

All four are in `daily.err`, all the same DarkWake cause, and the count of
applied actions is what separates a real dirty world from a false one:

| sim day | actions applied | world actually dirty |
|---|---|---|
| 42 | 2 | yes |
| 48 | 34 | yes, and it left day 48's snapshot 3 issues short |
| 53 | 0 | no |
| 67 | 0 | no |

Day 48 is the lasting damage: it is the source-break restoration day, the
converge failed after re-labelling 34 stories, and the snapshot that followed
caught only 32 of 35 issues. `scope-change-pct` recorded 3.7 against the
ledger's 11.85 and `blocked-share-pct` 22.58 against 18.92. The measure is
right and the snapshot is short, so no recompute can repair it — the day's raw
data is simply incomplete, and Jira has long since moved on.
