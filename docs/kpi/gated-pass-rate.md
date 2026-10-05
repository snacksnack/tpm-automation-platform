# Gated pass rate: the trip compares runs, not days (RC1-498)

## What went wrong

The weekly brief of 2026-10-05 led with "gated pass rate fell 46.7 points to
33.3 % and tripped" and asked for a decision on pinning the model and
freezing prompt changes on work-breakdown. It was one noisy run.

- The reading was a single `work-breakdown` run on 2026-09-28. The subject
  has three cases, so one failed case is 33 points. Two cases each failed one
  check: 1 of 25 citations not verbatim from the PRD, and 9 tasks against a
  limit of 8.
- Model, prompt hash and code version matched the three weekly runs before
  it, all 3/3. The next run, 2026-10-05, was 3/3.

Two things turned one run into a week-long trip and a brief asking for
action.

## 1. The trip rule counted one run twice

The adopted tree commits to "a billed subject under 80 % on two consecutive
measurements". `gated_pass_rate` implemented that as today's program value
and yesterday's both under the floor. The daily job re-reads the same weekly
run every day, so one bad run satisfied "two consecutive" the morning after
it landed and held until the next sweep.

Now a measurement is a run. The KPI trips when any billed subject's latest
run and the scorable run before it are both under the floor.

- **Any subject, not only the minimum's.** The freeze is per repo, so the
  worst subject being on its first bad run must not hide another on its
  second.
- **A run that scored nothing is skipped** when looking for the previous
  measurement. An all-errored run measured nothing.
- **The value is unchanged**: still the minimum across billed subjects. Only
  `tripped` and the detail text moved, so the tree and the rubric text stand
  and the rubric version stays at 2. The code now does what the tree said.

Replayed over all 51 stored eval-run-store snapshots (2026-08-23 on):

| rule | snapshots tripped | what tripped them |
|---|---|---|
| day over day (old) | 29 | includes all of 09-30 through 10-05, from the one 09-28 run |
| run over run (new) | 8 | 08-23 through 08-27 only: stakeholder-status-email, under the floor on two runs running |

The eight that remain are a real repeat, and they are the "not the minimum's
subject" case: the minimum those days was spec-review at 50 %, on its first
bad run.

## 2. The detail now carries case counts

`work-breakdown 33 % (1/3)` instead of `work-breakdown 33 %`. A reader, and
the model writing the brief, can see that the number is one case out of
three. When a subject is under the floor but has not tripped, the detail says
so: `under 80 % on one run only, not yet a trip: work-breakdown`.

## 3. The brief was written before the sweep it should report

The weekly brief ran Monday 08:00; the weekly eval sweep runs Monday 09:30.
Every brief described quality from runs seven days old and about to be
replaced.

The choice: the brief moves to Monday 10:30 and `scripts/kpi_weekly.sh`
re-reads the eval store itself before writing (snapshot, then track, for
eval-run-store only). It also waits, up to 45 minutes, for a sweep that is
mid-run. Moving the sweep ahead of the 07:00 daily job would have needed no
script change, but 2026-10-05 was also the morning the 07:00 job ran on a
sleeping laptop and timed out on every network call; a billed sweep should
not move into that window.

The launchd schedule is a file on the laptop. After merging, reload it with
the three commands in the plist's header.

## Not changed

The flaky checks themselves (`traces-to-the-prd`, `shows-restraint`,
`restates-the-computed-severity`) and the three-case subjects. Whether a
1-of-25 citation miss should fail a case belongs to the subjects' repos.
The `kpi-ledger` eval covers the simulated program only and is unaffected.
