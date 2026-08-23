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
| `tick.log`, `tick.err` | stdout / stderr of the launchd tick |
| `snapshot.log`, `snapshot.err` | stdout / stderr of the daily snapshot (RC1-301), if installed |

`python -m simulate status` prints the clock in one line.

Seeded on 2026-08-23: epic **PMA-167**, thirty stories (the GA sign-off
`p-ga` is PMA-173). Sim-day 0 is 2026-09-07; GA is committed for day 67.

## The daily tick

`scripts/launchd/com.reidcollins.kpi-sim-tick.plist` runs
`python -m simulate tick` from the repo checkout at **07:00 local** every
day. Installed and loaded on 2026-08-23, so day 1 lands on 2026-08-24 and
GA day 67 around the end of October 2026. The agent is not installed
automatically by anything in the repo — it changes what the machine does
every morning, so it is a deliberate step:

```bash
cp scripts/launchd/com.reidcollins.kpi-sim-tick.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.reidcollins.kpi-sim-tick.plist
launchctl list | grep kpi-sim        # "-  0  com.reidcollins.kpi-sim-tick" = loaded
```

How launchd behaves, and what it means for the program:

- **A missed 07:00 runs at wake.** If the laptop is asleep at seven, the
  tick fires as soon as it wakes — once. A day asleep is one missed day,
  not a burst of ticks; the sim-day simply lags the calendar by one per
  day missed. Catch up by hand (`tick --days N`) if that matters.
- **One tick per calendar day**, never more: the plist uses
  `StartCalendarInterval`, not `StartInterval`. Switch to
  `StartInterval 3600` for hourly ticks during development (edit the copy
  in `~/Library/LaunchAgents`, then `unload` and `load`).
- **It reads `.env`** from the working directory for Jira credentials, the
  same as a manual run.
- **It stops itself at day 69.** `tick` exits 1 with "the program's last
  day — nothing to advance" and makes no Jira calls; the agent keeps
  firing harmlessly until unloaded.

The daily **snapshot** (`com.reidcollins.kpi-snapshot.plist`, 07:30) is the
tick's companion: it records the converged day in the snapshot store. Same
install steps; [`snapshots.md`](snapshots.md) has it.

Check on it:

```bash
python -m simulate status              # where the clock is
tail -3 data/kpi-sim/tick.log          # the last ticks
cat data/kpi-sim/tick.err              # empty when healthy
python -m simulate verify              # does Jira match the scenario for today?
```

`verify` exits 1 and lists the differences if someone edited a simulated
issue by hand; the next tick converges it back (a converge is a diff, not a
replay — see `simulate/apply.py`).

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
launchctl unload ~/Library/LaunchAgents/com.reidcollins.kpi-sim-tick.plist   # stop the clock
python -m simulate teardown --dry-run                                       # what would go
python -m simulate teardown                                                 # delete every simulated issue, forget the clock
```

Teardown needs *Delete Issues* on PMA, which the default software scheme
grants only to the project's Administrators role (Reid was added to it on
2026-08-22). Unload the agent *before* tearing down, or the next morning's
tick finds no clock, exits 1, and writes "no clock — run `seed` first" to
`tick.err` every day until you notice.

Re-seeding after a teardown creates fresh keys; `manifest.json` is
rewritten, and anything that stored the old keys (a snapshot store, a
brief) is pointing at deleted issues.
