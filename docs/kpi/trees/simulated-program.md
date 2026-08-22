# KPI tree — Observability Platform GA (simulated) — hand-written baseline

Drafted by hand under [rubric v1](../rubric.md) **before** the define stage
ran, from the [program brief](../programs/simulated-program.md). This is the
baseline the agent's draft is argued against; the review lives in
[`simulated-program.review.md`](simulated-program.review.md).

Unlike the brief, this document knows about the simulator's planted events
(RC1-299) — scope add in week 3, upstream slip in week 5, cost spike in week 6,
silent source break in week 7 — because the baseline is also the specification
the ground-truth ledger (RC1-300) is derived from. Each KPI says which planted
event must move it, and by when.

## Shape

```
O1  Forecast slip (days)             schedule outcome
    L1  Scope change vs baseline (%)
    L2  Critical-path slack (days)
    L3  Blocked share of open work (%)
    L4  Throughput, trailing 2 weeks (points/week)   [activity-derived]
O2  Cost vs envelope (USD, %)        unit-economics outcome
    (L4 is the only leading input to O2 in this program: spend is driven by
     the telemetry pipeline going live, which throughput predicts)
```

## Outcomes

### O1 — `forecast-slip-days` · Forecast slip

| | |
| --- | --- |
| type | outcome · lagging |
| direction | lower is better; zero means the committed date holds |
| unit | days |
| definition | `forecast_ga − committed_ga`, where `forecast_ga = today + remaining_points ÷ trailing_throughput_per_day`. `remaining_points` is the sum of story points on stories not in Done; `trailing_throughput_per_day` is points moved to Done over the last 14 sim-days ÷ 14. Before 14 days of history exist, throughput is the plan rate (`total_points ÷ 70`). If throughput is zero, the forecast is **undefined** and reported as such, never as zero slip. |
| source | Jira snapshot: `status`, story points, epic `duedate` (committed GA); program calendar for today. Daily. Owner: TPM. |
| stale_after | 2 sim-days |
| so_what | If the forecast slips past **+5 days**, the decision is cut scope or add a person to the critical workstream; past **+10**, the GA date is re-committed upward. Under +5 the date holds and nothing changes. |
| goodhart | **high.** Gaming path: move stories to Done that are not done (remaining points fall, forecast improves). Counter: reopen rate — stories leaving Done — reported beside it; any reopen in a week flags the forecast. |
| failure_modes | throughput window straddles a holiday or a week of onboarding (forecast swings on a small denominator); points re-estimated downward mid-flight (remaining falls without work); stale snapshot re-read as fresh; **denominator collapse** when few stories remain and one closure moves the forecast by days. |
| planted | scope add (wk 3) raises it within 1 day via remaining points; upstream slip (wk 5) raises it within 14 days as throughput on the blocked chain falls — the slack indicator (L2) is what sees it on day 1. |

### O2 — `cost-vs-envelope` · Cloud cost vs envelope

| | |
| --- | --- |
| type | outcome · lagging |
| direction | lower is better; reported as cumulative actual vs cumulative plan |
| unit | USD and % of plan-to-date |
| definition | `cumulative_actual − cumulative_plan` through the last complete week, and `cumulative_actual ÷ cumulative_plan`. Also the latest week's actual vs its plan, because the cumulative figure hides a single bad week for a while. |
| source | cloud spend line: `week, planned_usd, actual_usd`. Weekly, lands the Monday after. Owner: TPM (simulator: scenario module). |
| stale_after | 8 days (one cadence plus a day) |
| so_what | Over **110 %** of plan-to-date for two consecutive weeks: the decision is re-scope the pipeline (sampling, retention) or go back to Finance for the envelope. A single-week spike over 150 % of its plan is escalated the week it lands. |
| goodhart | **medium.** Gaming path: defer ingest of a service to next quarter so this program's line looks fine. Counter: scope change (L1) — a service dropped from scope shows there. |
| failure_modes | the spend line is a **synthetic** feed in the simulation and a billing export in reality — both are weekly and both can silently stop, so staleness here is the common case, not the edge; plan-to-date uses the week calendar and a misaligned week boundary mis-states %; cost spike attributed to the program that was actually another team's. |
| planted | cost spike (wk 6): the week-6 actual lands at ~2× plan; O2 must read it the Monday after week 6 and O2's single-week line must trip the 150 % escalation. |

## Leading indicators

### L1 — `scope-change-pct` · Scope change vs baseline

| | |
| --- | --- |
| type | leading → O1 |
| direction | lower is better; zero means scope is what was committed |
| unit | % of baseline points |
| definition | `(points_added − points_removed) ÷ baseline_points`, where baseline is the sum of story points on stories that existed at kickoff (sim-day 0 snapshot) and `added` counts stories created after kickoff plus upward re-estimates. Reported as net and gross (added and removed separately). |
| source | Jira snapshot: `created`, story points, compared to the day-0 snapshot. Daily. Owner: TPM. |
| stale_after | 2 sim-days |
| leads | O1 — mechanism: points added today are days of work the forecast has not absorbed yet; lead time: the forecast (O1) moves the same day via remaining points, but the *sponsor's* decision lead time is what L1 buys — a 15 % add with 7 weeks left is recoverable; the same add with 2 weeks left is not. |
| so_what | Net scope over **+10 %** triggers a scope review with the sponsor that week: which stories are GA-blocking and which are not. |
| goodhart | **low.** Splitting one story into three at the same points does not move it; hiding scope by not filing it is the gaming path and is caught by O1 when the unfiled work has to be done anyway. |
| failure_modes | stories created after kickoff that are *re-filed* duplicates read as adds; stories moved out of the epic read as removals; unestimated stories are invisible (report the count of unestimated open stories beside it). |
| planted | scope add (wk 3): +4 stories, ~+15 % points; L1 must cross +10 % the day they are created. |

### L2 — `critical-path-slack-days` · Critical-path slack

| | |
| --- | --- |
| type | leading → O1 |
| direction | higher is better; negative means the schedule is already inverted |
| unit | days |
| definition | Over every `blocks` link, `slack = downstream.start − upstream.due`; the KPI is the **minimum** across links on the path to any GA-blocking story, with the chain named. Uses the platform's dependency DAG (`drift/graph.py`) and the same inversion rule the drift detector ships. |
| source | Jira snapshot: `blocks` links, start date, due date. Daily. Owner: TPM. |
| stale_after | 2 sim-days |
| leads | O1 — mechanism: a due-date slip on an upstream consumes slack first; the forecast only moves once the downstream actually fails to start and throughput on that chain falls. Lead time: the slack itself — at +10 days of slack a slip is invisible to O1 for ~10 days. |
| so_what | Slack under **3 days** on a GA-blocking chain: the TPM re-sequences or re-assigns that week. Negative slack: the downstream's dates are re-planned immediately. |
| goodhart | **medium.** Gaming path: push downstream start dates out to manufacture slack (which is just slipping the schedule quietly). Counter: O1 — pushed starts flow into remaining work timing. |
| failure_modes | links entered backwards (`blocks` direction wrong) invert the sign — the seeder verifies direction for exactly this reason; stories without start dates fall out of the minimum silently (report how many links were skipped); a link to a story outside the epic is ignored. |
| planted | upstream slip (wk 5): an upstream `duedate` moves +14 days; L2 must go negative on that chain the same sim-day. |

### L3 — `blocked-share-pct` · Blocked share of open work

| | |
| --- | --- |
| type | leading → O1 |
| direction | lower is better |
| unit | % of open points |
| definition | Points on open stories whose status is Blocked, **or** whose upstream (transitively, via `blocks`) is Blocked or past due and not Done, ÷ all open points. The direct and transitive halves are reported separately. |
| source | Jira snapshot: `status`, `blocks` links, story points, due dates. Daily. Owner: TPM. |
| stale_after | 2 sim-days |
| leads | O1 — mechanism: blocked points are points that will not convert to throughput this window; a rising blocked share precedes a falling trailing throughput by up to the window length. Lead time: 7–14 days. |
| so_what | Blocked share over **25 %** for three consecutive days: the TPM's weekly ask to the sponsor names the blocker owner; that is the escalation, not a dashboard colour. |
| goodhart | **medium.** Gaming path: leave a blocked story In Progress rather than mark it Blocked. Counter: the transitive half catches it when the upstream is late; and days-in-status on In Progress stories (a diagnostic, not a KPI) is in the drill-through. |
| failure_modes | the status name `Blocked` is workflow-specific — a project without that status makes the direct half read zero (which must be reported as *not measurable*, not zero); transitive blockage over-counts when a late upstream is late by a day. |
| planted | upstream slip (wk 5) makes the downstream chain transitively blocked once the old due date passes; L3 rises in week 5–6. |

### L4 — `throughput-points-per-week` · Throughput, trailing two weeks

| | |
| --- | --- |
| type | leading → O1 (and the only leading input to O2) · **activity-derived** |
| direction | higher is better, against the plan rate |
| unit | points per week |
| definition | Points moved to Done over the last 14 sim-days ÷ 2, compared with the plan rate (`total_points ÷ 10`) and with the previous 14-day window. |
| source | Jira snapshot: `status` transitions inferred by diffing consecutive daily snapshots (not the changelog — see RC1-301), story points. Daily. Owner: TPM. |
| stale_after | 2 sim-days |
| leads | O1 — mechanism: it *is* the denominator of the forecast; it is listed as a leading indicator rather than folded into O1 so that the sponsor can see whether a forecast move came from scope (L1) or from pace (L4). Leads O2 — mechanism: telemetry spend ramps as tracing stories land; throughput on the tracing workstream predicts the spend line by about a week. |
| so_what | Trailing throughput under **70 % of plan rate** for two windows: the decision is staffing (add a person) or scope, not "work harder" — the brief says which workstream. |
| goodhart | **high** — it is an activity count. Gaming path: close, split, or inflate points. Counter: O1 with reopen rate; and L1 gross adds (inflation shows as re-estimates). This is why it is not an outcome. |
| failure_modes | the 14-day window is noisy on a 5-person team — one large story closing swings it; holidays; re-estimates after Done. |
| planted | none directly; falls in week 5–6 as the slipped chain stalls. |

## Rejected candidates

| Candidate | Ground | Reason |
| --- | --- | --- |
| Stories closed per week | activity at the root | It is the team's output count, not the program's result; it is the input to L4 and lives there, labelled. |
| Velocity (points per sprint) | activity / duplicate | Same information as L4 with a sprint boundary the program does not use. |
| % stories with comments in the last 7 days ("engagement") | no decision | The sponsor changes nothing on it; it is a TPM diagnostic for the drill-through. |
| Open drift findings (count) | diagnostic / duplicate | The drift detector's alert count; its *content* feeds L2, its count changes no decision. |
| Days since last status update per story | no decision | Drill-through for a stalled-story conversation; not SVP altitude. |
| On-call alert volume after GA | unmeasurable in-program | The real outcome of an observability platform, and not observable until after GA — recorded here so the successor program knows to measure it. |

## Source break (week 7) — what the tree must do

The simulator drops the label the collector keys on (RC1-299). Every
Jira-sourced KPI (O1, L1–L4) loses its issues in one tick. The correct reading
is **broken**, not a forecast of zero slip and 0 % scope change. The detection
rule the collector must implement (RC1-301 / RC1-307): the issue count under
the program falls by more than 50 % between consecutive snapshots with no
matching transitions or deletions in the window → mark the source broken,
mark the dependent KPIs stale from the last good snapshot, and carry the last
good value with its date. O2 is unaffected (different source) and must keep
reporting — the brief says so explicitly, so the blast radius is visible.
