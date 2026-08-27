# Escalate — acting on the unmeasurable (RC1-307)

The sixth stage, and the "program runs itself" half of the epic. Track
(RC1-305) already refuses to write a zero for a KPI it cannot measure —
the reading is `stale` or `broken` with a reason. This stage is what
happens *because* of that reading: retry the collector once, name the
blast radius, propose the fix, record it, and raise it — to Slack today,
and into the weekly brief (RC1-306) so it cannot scroll away.

Nothing here calls a model. Every detection is a rule over the snapshot
and the stored readings, because this stage runs unattended at 07:00 and
an escalation channel that can hallucinate is worse than none.

- Code: `kpi/escalate.py` (the stage), `kpi/escalations_store.py`
  (Postgres, beside the readings and the briefs).
- Runs last in `scripts/kpi_daily.sh`: tick → snapshot → track → escalate.

```bash
python -m kpi.escalate --program simulated-program             # detect, retry, record, post
python -m kpi.escalate --program eval-run-store --no-post      # record but stay off Slack
python -m kpi.escalate --program simulated-program --dry-run   # print only — no retry, no stores
```

Exit codes mirror the other stages: **0** nothing stands (a problem healed
on retry counts as nothing standing), **1** at least one escalation
stands, **2** the stage could not run.

## The four detections

| kind | rule | proposed fix |
| --- | --- | --- |
| `source` | a source read `error`; read `missing` after having answered before; or answered with under half its last good row count (`SOURCE_BREAK_DROP`, the measures' own rule) | names the credential, the label/JQL, or the origin to inspect |
| `reading` | a KPI read `broken` for a reason no source escalation explains — a raised measure is what a shape change looks like from here | re-run `python -m kpi.instrument`; a changed shape invalidates the verification, not just the number |
| `flatline` | an `ok` value unchanged past twice its declared `stale_after` (floor: 7 daily readings) | confirm the source is actually updating — a stuck sensor reads like a healthy metric |
| `implausible` | an `ok` value outside what its unit allows: a share of cases past 100 %, negative dollars per run | do not trust the reading; re-verify the measure against the snapshot |

Three deliberate exemptions, all against false alarms — the failure mode of
an escalation channel is not silence but unsubscribing:

- A source that has **never answered yet** is not an escalation. The
  simulated program's spend line is legitimately empty until week 1; the
  cost KPIs already read stale and say why. "Answered for six weeks, then
  nothing" is the week-7 break; the health model exists to tell the two
  apart.
- A KPI **resting at its own ideal boundary** is not a flatline. An error
  rate at 0 for a month is a program behaving; so is a pass rate at 100.
- Bounds trip on **impossible, never surprising**: 204 % of plan is a real
  overspend, not a broken measure, so a percentage *of a reference* and a
  signed difference are left unbounded. Surprising is `tripped`'s job.

## The retry, and what "healed" means

A `source` escalation triggers one collector re-run — the same wiring as
`python -m collectors snapshot`. The fresh snapshot is kept **only if the
re-detection actually clears something**: then it is stored, the day is
re-tracked against it, the healed readings replace the day's row in
`kpi_readings` (last word wins, as always), and the escalation is recorded
with `healed = true`. A retry that changes nothing stores nothing — the
escalation stands and says so.

Recovery is recorded rather than discarded because "jira was down at 07:00
and back at 07:01" is a fact about the program's morning, and the brief
reports it as recovery rather than silence.

## Where escalations land

`kpi_escalations`, same Postgres as the readings (RC1-304). One row per
(program, day, kind, subject) — `subject` is the source name or the KPI id;
`kpi_ids` is the blast radius, computed from the instrument report's cited
fields, not from anyone's memory. The table refuses a row with no reason or
no proposed fix, the same constraint style `kpi_readings` uses against
reasonless staleness.

Slack gets the message the day it happens, through the same webhook the
drift detector uses. A break that persists does not repost every morning:
`already_raised` checks for a posted, un-healed row for the same (kind,
subject) within seven days, so a standing break reposts weekly. The weekly
brief reads the week's rows and renders them in full — reason, blast
radius, fix — under *Escalations this week*; the narrate prompt (v2) tells
the model to weigh them in `movement` and `asks` but never to restate,
soften, or invent one.

## The done-when, as tests

`tests/test_kpi_escalate.py` runs the scenario to day 43 through
`simulate.collected` — the planted silent break, seen exactly as the
collector would store it:

- the break is caught **on the day it happens**, from the snapshot alone;
- the blast radius is the four Jira KPIs the instrument report cites;
- the affected readings read broken with reasons and **no zero anywhere**;
- day 42 escalates nothing — the spike is a tripped threshold, not a break;
- and a partial drop (label gone from most stories, health still `ok`) is
  caught by the count rule the measures share.
