# KPI tree — Observability Platform GA — agent draft

Drafted under rubric v1, define prompt v1, model `claude-opus-4-8`. Generated; review against the hand-written baseline before adopting.

**Sponsor question, as understood:** Will we hit the committed GA date, will we stay inside the agreed ten-week cloud-cost envelope, and will the sponsor find out early enough to act if either starts to slip?

## Shape

```
forecast-slip-days                 GA date forecast slip
    blocked-critical-path-points   Blocked story points on the dependency chain
    throughput-vs-required         Throughput gap vs required run-rate  [activity-derived]
    reopen-rate                    Story reopen rate
cost-envelope-variance             Cloud-cost envelope variance
    weekly-spend-burn-ratio        Weekly spend burn ratio
```

## Outcomes

### `forecast-slip-days` · GA date forecast slip

| | |
| --- | --- |
| type | outcome |
| direction | lower is better |
| unit | days |
| definition | On the current daily snapshot, compute a forecast completion date for the program's committed scope and subtract the epic's committed GA date. Forecast date = last committed working day required to complete all remaining (not-Done) story points at the trailing throughput rate, where throughput = sum of story points of stories that moved to Done in the last 28 calendar days divided by 28, then multiplied out over remaining working days respecting the calendar's week boundaries. Result is (forecast date - committed GA date) in calendar days; positive means late, negative means early. If trailing throughput is zero (no points completed in 28 days), do not report zero: report the KPI as broken with reason 'no completed work to forecast from'. |
| source | Jira issues under the program epic + Jira epic + Program calendar: Jira issues under epic PMA-2: status, story points, resolutiondate (Done transitions) from daily snapshots for trailing 28 days; remaining points = sum of story points where status != Done; committed GA date = epic duedate; week boundaries from Program calendar. daily. Owner: TPM. |
| stale_after | 2 days |
| so_what | If forecast slip exceeds +5 days, the sponsor cuts scope (drop one workstream's stretch stories) or moves the GA date rather than waiting for week 10 to confirm the miss. |
| goodhart | **high.** Gaming path: Mark stories Done that are not actually complete to inflate trailing throughput and pull the forecast in. Counter: reopen-rate: fraction of stories reopened within 14 days of being marked Done. |
| failure_modes | Denominator collapse: if remaining scope shrinks because stories are deleted or de-scoped silently, the forecast improves without work being done.; Stale source: daily snapshot collector stops and yesterday's forecast is re-read as today's.; Survivorship: stories never created for known-but-unplanned work make remaining scope look smaller than reality.; Throughput whiplash: a 28-day window early in a 10-week program is dominated by ramp-up and reads pessimistically. |

### `cost-envelope-variance` · Cloud-cost envelope variance

| | |
| --- | --- |
| type | outcome |
| direction | lower is better |
| unit | USD |
| definition | Cumulative actual telemetry-pipeline spend to date minus cumulative planned spend to date, taken from the weekly cloud spend line. Numerator is sum of actual spend rows through the latest available week; subtract sum of planned spend rows through the same week. Positive means over budget. If no spend row is yet available for any week (program week 0), report the KPI as stale with reason 'no spend row yet', never zero. |
| source | Cloud spend line: Cloud spend line: sum(actual spend) - sum(planned spend) over all weeks with rows available; envelope = agreed ten-week planned total. weekly (Monday after the week). Owner: TPM (Finance owns the source row). |
| stale_after | 9 days |
| so_what | If cumulative variance exceeds +10% of the ten-week envelope, the sponsor changes the telemetry retention/ingest configuration or renegotiates the envelope with Finance rather than absorbing an overrun at GA. |
| goodhart | **medium.** Gaming path: Defer ingest/storage config changes so spend lands in a later week, keeping cumulative variance flat until after a checkpoint. Counter: forecast-slip-days: deferring pipeline work to suppress spend shows up as schedule slip. |
| failure_modes | Stale source: weekly row is late (available Monday after), so the number lags reality by up to a week around checkpoints.; Denominator/plan drift: if planned spend rows are revised mid-program, variance is measured against a moving baseline.; Instrumentation hides the outcome: spend attributed to another cost line outside the telemetry pipeline row understates true program cost. |

## Leading indicators

### `blocked-critical-path-points` · Blocked story points on the dependency chain

| | |
| --- | --- |
| type | leading → forecast-slip-days |
| direction | lower is better |
| unit | story points |
| definition | On the current daily snapshot, sum of story points of not-Done stories that are currently 'is blocked by' at least one not-Done story, counting only stories on the tracing->SLO->alerting dependency chain (any story reachable via blocks links to a tier-1 deliverable). Denominator-free absolute count. If no blocks links exist in the snapshot, report zero blocked points explicitly (a real zero here, distinct from stale); if the snapshot is missing, report stale. |
| source | Jira issues under the program epic: Jira issues under PMA-2: status, story points, 'is blocked by'/'blocks' links from the daily snapshot; filter to not-Done on either end. daily. Owner: TPM. |
| stale_after | 2 days |
| so_what | If blocked points on the critical path exceed 8 for more than 3 consecutive days, the sponsor reassigns an engineer onto the blocking workstream before the block converts into forecast slip. |
| leads | forecast-slip-days — mechanism: Blocked points on the dependency chain cannot be completed until the blocker resolves, so throughput on downstream workstreams stalls and the forecast date moves out; blockage rises before completions fall. Lead time: 1-2 weeks |
| goodhart | **medium.** Gaming path: Remove 'is blocked by' links without resolving the underlying dependency so the number drops while work is still stuck. Counter: forecast-slip-days: unlinking without progress does not improve throughput, so slip persists. |
| failure_modes | Instrumentation hides the outcome: real dependencies not modelled as blocks links read as zero blockage.; Stale source: snapshot collector stops and stale block state re-read.; Survivorship: blocks only recorded once work starts, so early unstarted dependencies are invisible. |

### `throughput-vs-required` · Throughput gap vs required run-rate

| | |
| --- | --- |
| type | leading → forecast-slip-days · **activity-derived** |
| direction | higher is better |
| unit | story points per week |
| definition | Trailing 28-day completed story points per week (sum of story points of stories moved to Done in last 28 days divided by 4) minus the required run-rate (remaining not-Done story points divided by working weeks remaining until committed GA date). Positive means ahead of the rate needed. If working weeks remaining is zero (at or past GA), report the KPI as stale with reason 'past GA horizon' rather than dividing by zero. |
| source | Jira issues under the program epic + Program calendar: Jira daily snapshots: story points Done in trailing 28 days; remaining points = sum(story points where status != Done); weeks remaining from Program calendar and epic duedate. daily. Owner: TPM. |
| stale_after | 2 days |
| so_what | If the throughput gap is negative for two consecutive weeks, the sponsor adds an engineer or cuts scope now rather than at week 8 when it is unrecoverable. |
| leads | forecast-slip-days — mechanism: The forecast date is a function of throughput against remaining scope; a sustained gap below the required rate mechanically produces a slip before the slip fully materialises in the forecast. Lead time: 2-3 weeks |
| goodhart | **high.** Gaming path: Complete many small low-value stories to lift trailing throughput while the hard critical-path stories remain open. Counter: blocked-critical-path-points: padding easy stories leaves critical-path blockage untouched. |
| failure_modes | Denominator collapse: as weeks remaining shrinks, required run-rate spikes and the gap swings sharply late in the program.; Estimation drift: story points re-estimated mid-flight change both terms without real progress.; Survivorship: unestimated stories excluded from remaining points understate what is required. |

### `reopen-rate` · Story reopen rate

| | |
| --- | --- |
| type | leading → forecast-slip-days |
| direction | lower is better |
| unit | % |
| definition | Numerator: count of stories that transitioned from Done back to a not-Done status within 14 days of being marked Done, observed across daily snapshots in the trailing 28 days. Denominator: count of stories marked Done in that same trailing 28-day window. Rate = numerator/denominator. If the denominator is zero (nothing marked Done in the window), report the KPI as stale with reason 'no completions to assess', never zero. |
| source | Jira issues under the program epic: Jira daily snapshots over trailing 28 days: detect Done->not-Done status changes by comparing consecutive snapshots; denominator = stories reaching Done in window. daily. Owner: TPM. |
| stale_after | 2 days |
| so_what | If reopen rate exceeds 15%, the sponsor freezes the 'Done' definition and requires a review gate, because the forecast-slip and throughput numbers are being inflated by false completions. |
| leads | forecast-slip-days — mechanism: A rising reopen rate means completions counted toward throughput are not real, so the forecast is optimistic; the reopen signal appears before the re-work re-inflates remaining scope and the forecast corrects. Lead time: 1-2 weeks |
| goodhart | **low.** Gaming path: none Counter: none |
| failure_modes | Instrumentation hides the outcome: reopens done by cloning a new story instead of reverting status never show as a reopen.; Denominator collapse: few completions in the window make the rate jumpy and noisy.; Snapshot-only detection misses same-day reopen-and-reclose that never appears in a daily snapshot. |

### `weekly-spend-burn-ratio` · Weekly spend burn ratio

| | |
| --- | --- |
| type | leading → cost-envelope-variance |
| direction | lower is better |
| unit | ratio |
| definition | For the latest available spend week, actual spend divided by planned spend for that same week (single-week, not cumulative). A value of 1.0 is on plan; above 1.0 is overspending this week. If planned spend for the week is zero, do not report zero or infinity: report the KPI as broken with reason 'no planned baseline for week'. |
| source | Cloud spend line: Cloud spend line latest available week row: actual spend / planned spend. weekly (Monday after the week). Owner: TPM (Finance owns the source row). |
| stale_after | 9 days |
| so_what | If the weekly burn ratio exceeds 1.2 for two consecutive weeks, the sponsor changes telemetry retention/sampling configuration now to protect the envelope, before cumulative variance breaches 10%. |
| leads | cost-envelope-variance — mechanism: Cumulative variance is the running sum of weekly gaps; a weekly burn ratio above 1.0 adds to cumulative variance before the cumulative figure crosses its threshold, so the weekly ratio moves first. Lead time: 2-4 weeks |
| goodhart | **medium.** Gaming path: Shift ingest/storage load into a later week to keep any single week's ratio near 1.0 while total spend is unchanged. Counter: cost-envelope-variance: single-week smoothing leaves cumulative variance visible. |
| failure_modes | Stale source: weekly row available only the Monday after, so overspend is seen up to a week late.; Plan drift: mid-program revisions to planned weekly spend reset the baseline and mask a trend.; Single-week noise: a one-off backfill spike reads as a burn problem when cumulative spend is fine. |

## Rejected and proxied candidates

| Candidate | Ground | Reason |
| --- | --- | --- |
| Stories closed per week (velocity) | activity | A raw count of stories the team closed; doubling it by closing trivial or padded stories would not please the sponsor, who committed a date and a cost. It is activity at the root. Its useful form lives beneath the outcome as throughput-vs-required. |
| Comments per story / team communication volume | activity | Comment counts measure effort and discussion, not whether the date or cost will hold, and change no sponsor decision. Pure activity with no mechanism to either outcome. |
| Trace coverage across backend services (% of services emitting traces) | unmeasurable | This is arguably the truest product-quality outcome for an observability platform, but no source in the brief records runtime trace emission per service; only Jira status, spend, and calendar exist. An honest proxy would be completion of tracing-workstream stories, which is already captured by the schedule outcome and would double-count. **Proxy:** Fraction of tracing-workstream stories marked Done in Jira **Misses:** A story marked Done does not prove the service actually emits usable traces in production; it misses instrumentation that ships but does not work, which is precisely the quality gap the direct measure would catch. |
| On-call alert response time / SLO breach rate | unmeasurable | Whether alerting actually routes and SLOs actually hold is the operational outcome, but the brief exposes no alerting or SLO telemetry source, only Jira, spend, and calendar. No honest proxy exists that is not just schedule completion. |
| Percent of stories with populated start and due dates | diagnostic | Useful for trusting the forecast's inputs (data hygiene), but the sponsor would not add people, cut scope, or move a date on it directly. It belongs in the drill-through as a health check on forecast-slip-days, not at SVP altitude. |

## Notes

- The rubric caps the tree at two outcomes and four leading indicators; the two truest product outcomes for an observability platform (trace coverage, alert/SLO health) have no source in the brief and were rejected as unmeasurable rather than proxied through schedule data, per the proxy rule that a proxy whose divergence duplicates an existing KPI is a rejected KPI.
- Both outcomes match the sponsor's stated concern verbatim: the GA date (forecast-slip-days) and the cost envelope (cost-envelope-variance). The sponsor's third concern, 'find out early', is served by the four leading indicators rather than a separate outcome.
- throughput-vs-required is marked activity_derived=true because its numerator is a count of work the team did; the rubric permits this beneath an outcome and it is paired with reopen-rate and blocked-critical-path-points to counter the gaming path of padding easy stories.
- All Jira-derived KPIs depend on the daily snapshot and on blocks links / dates being maintained; where instrumentation is absent (unmodelled dependencies, clone-instead-of-reopen) the numbers read falsely clean, which is recorded in failure_modes and is the main risk the instrument stage (RC1-303) should verify.

