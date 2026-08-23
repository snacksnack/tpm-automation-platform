# Running the simulated program — the runbook

The Observability Platform GA program (RC1-299) is a scripted ten-week
program in the PMA Jira project that advances one simulated day per tick.
This is how it is run on the dev machine: the clock, the daily tick, what
to do when it stalls, and how to take it down. What the program *is* —
stories, planted events, the ground-truth ledger — is in
[`trees/simulated-program.md`](trees/simulated-program.md) and
[`ledger.md`](ledger.md).

## State

Everything lives in `data/kpi-sim/` (gitignored — machine state, not repo
artifacts):

| file | what |
| --- | --- |
| `clock.json` | current sim-day and sim-date, week, active events, whether the source is broken, last tick time |
| `manifest.json` | slug → Jira key for the epic and every story the last converge saw |
| `spend.csv` | the weekly cloud-spend line, only the weeks that have landed by the current day |
| `ledger.csv` | the ground-truth ledger for the whole program (RC1-300); rewritten on every converge |
| `daily.log`, `daily.err` | stdout / stderr of the daily run (tick, then snapshots) |

`python -m simulate status` prints the clock in one line.

Seeded on 2026-08-23: epic **PMA-167**, thirty stories (the GA sign-off
`p-ga` is PMA-173). Sim-day 0 is 2026-09-07; GA is committed for day 67.

## The daily run

`scripts/launchd/com.reidcollins.kpi-daily.plist` runs
`scripts/kpi_daily.sh` at **07:00 local** every day: **tick** the simulated
program one day, then **snapshot** `simulated-program` and `eval-run-store`
into the store (RC1-301). One job rather than one per step, so the order —
converge first, record second — holds even when a missed morning fires at
wake. Installed and loaded on 2026-08-23 (it replaced the tick-only agent
the same day), so day 1 lands on 2026-08-24 and GA day 67 around the end of
October 2026. Nothing in the repo installs it; it is a deliberate step:

```bash
cp scripts/launchd/com.reidcollins.kpi-daily.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.reidcollins.kpi-daily.plist
launchctl list | grep kpi-daily          # "-  0  com.reidcollins.kpi-daily" = loaded
```

**Credentials.** Jira comes from the repo's `.env` (config reads it).
`EVAL_DATABASE_URL` has exactly one home, `~/.zshrc` (RC1-263), and launchd
does not read shell profiles — so the script pulls that one `export` line
out of the profile itself. Nothing to configure, nothing copied into the
plist; when Heroku rotates the credential, update `~/.zshrc` and the next
morning picks it up. With the line absent, the eval-run-store snapshot
records its source as `error` and the run carries on.

How launchd behaves, and what it means for the program:

- **A missed 07:00 runs at wake.** If the laptop is asleep at seven, the
  job fires as soon as it wakes — once. A day asleep is one missed sim-day,
  not a burst; the sim-day simply lags the calendar by one per day missed.
  Catch up by hand (`python -m simulate tick --days N`, then
  `scripts/kpi_daily.sh --no-tick`) if that matters.
- **One run per calendar day**, never more: `StartCalendarInterval`, not
  `StartInterval`.
- **The tick stops itself at day 69** (exit 1, no Jira calls); the
  snapshots keep running and the agent keeps firing harmlessly until
  unloaded.
- **Exit 1 is normal during week 1**: the spend line has no landed week
  until day 7, so the simulated program's snapshot reports `spend missing`
  — recorded, as it should be, and not a fault.

Check on it:

```bash
python -m simulate status                # where the clock is
tail -20 data/kpi-sim/daily.log          # the last run: tick, then each snapshot's health
cat data/kpi-sim/daily.err               # empty when healthy
python -m collectors runs simulated-program   # every stored day
python -m simulate verify                # does Jira match the scenario for today?
```

`verify` exits 1 and lists the differences if someone edited a simulated
issue by hand; the next tick converges it back (a converge is a diff, not a
replay — see `simulate/apply.py`).

`scripts/kpi_daily.sh --no-tick` takes today's snapshots without advancing
the clock — the way to record a day you jumped to.

## Jumping ahead, and back

```bash
python -m simulate to-day 45           # converge Jira straight to day 45
python -m simulate tick --days 7       # advance a week in one go
```

Both write the clock, so the next morning's tick advances from wherever you
left it. Jumping *back* (`to-day 10` from day 45) also converges — statuses
regress, the slipped due date is restored, scope-add stories stay present
with their labels (Jira keeps them; the scenario simply does not list them
as existing yet, and `verify` will not complain about extras). It is a
development convenience, not a clean reset; for that, tear down and seed.

Things that only make sense to test by jumping: the source break (days
43–47), the cost spike row landing (day 42), the slip (day 29). The
[ledger](ledger.md) says what every KPI should read on each.

## Stopping and tearing down

```bash
launchctl unload ~/Library/LaunchAgents/com.reidcollins.kpi-daily.plist      # stop the clock
python -m simulate teardown --dry-run                                       # what would go
python -m simulate teardown                                                 # delete every simulated issue, forget the clock
```

Teardown needs *Delete Issues* on PMA, which the default software scheme
grants only to the project's Administrators role (Reid was added to it on
2026-08-22). Unload the agent *before* tearing down, or the next morning's
tick finds no clock, exits 1, and writes "no clock — run `seed` first" to
`daily.log` every day until you notice.

Re-seeding after a teardown creates fresh keys; `manifest.json` is
rewritten, and anything that stored the old keys (a snapshot store, a
brief) is pointing at deleted issues.
