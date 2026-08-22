# KPI tree — Agent Eval Harness (agent-evals) — agent draft

Drafted under rubric v1, define prompt v1, model `claude-opus-4-8`. Generated; review against the hand-written baseline before adopting.

**Sponsor question, as understood:** For every shipped LLM system, how do I know its output is good enough to trust, at a cost I can see, so I can decide when to freeze prompts, switch models, add/retire cases, or stop measuring a subject?

## Shape

```
gating-pass-rate                   Gating characteristic pass rate (per subject)
    days-since-last-run            Days since last run (per subject)
    advisory-degradation-rate      Advisory characteristic degradation rate
    errored-case-share             Errored case share (per subject)
    unmeasured-code-versions       Unmeasured code-version drift (per subject)
cost-per-run                       Model spend per subject run
```

## Outcomes

### `gating-pass-rate` · Gating characteristic pass rate (per subject)

| | |
| --- | --- |
| type | outcome |
| direction | higher is better |
| unit | % |
| definition | Over the most recent run per subject in the trailing 7-day window: numerator = count of gating characteristic evaluations (characteristics[].advisory = false) where passed = true across all non-errored cases (results[].error is null); denominator = count of all gating characteristic evaluations across those same non-errored cases. If the denominator is zero (no non-errored gating evaluations exist for the subject in the window), the value is reported as the state 'no-signal', never 0%. Errored cases are excluded from both numerator and denominator and counted separately as the errored-case count that travels with this metric. |
| source | eval_runs (Postgres) + record.results[]: Latest run_id per subject within 7d by max(started_at); expand record.results[] where error IS NULL; for each characteristics[] where advisory=false, aggregate passed=true over total. per run (human-triggered; often weekly). Owner: Owner of the six systems (program sponsor). |
| stale_after | 14 days |
| so_what | If a subject's gating pass rate drops below 100% (any gating regression) or falls week-over-week by more than 5 points, the sponsor freezes prompt changes on that repo until the regression is resolved. |
| goodhart | **high.** Gaming path: Reclassify a failing gating characteristic as advisory, or retire the hard cases that fail, so the rate rises without the output improving. Counter: Track scored-case count and gating-characteristic count per subject alongside the rate; a rate rise paired with a drop in either is flagged. |
| failure_modes | Survivorship: errored cases produce nothing to score and silently leave the denominator, so a run that mostly crashed can show a high pass rate over the few cases that ran.; Denominator collapse: if cases are retired the rate rises over a shrinking base.; Stale source: store is silent unless a human runs a suite, so the 'current' rate may be from an old run.; Instrumentation hides the outcome: a characteristic that stops being emitted disappears from the denominator rather than reading as a failure. |

### `cost-per-run` · Model spend per subject run

| | |
| --- | --- |
| type | outcome |
| direction | lower is better |
| unit | USD |
| definition | For the most recent run per subject in the trailing 30-day window: sum of results[].usage.cost_usd across all cases in that run (including errored cases, which still consume tokens). Report per subject. If a run has no cost_usd populated on any case (denominator of scored cost = zero), recompute from usage.input_tokens/output_tokens against the model price table for the run's model id; if neither is available, report the state 'no-signal', never $0. |
| source | eval_runs (Postgres) + record.results[] + Model price table: Latest run_id per subject within 30d; sum results[].usage.cost_usd; fallback = (input_tokens*in_price + output_tokens*out_price)/1e6 by model id. per run. Owner: Owner of the six systems (program sponsor). |
| stale_after | 45 days |
| so_what | If cost-per-run for a subject rises above the sponsor's per-subject budget ceiling (e.g. exceeds the prior model's cost by >30%), the sponsor changes the model that subject runs on or retires expensive cases. |
| goodhart | **medium.** Gaming path: Lower cost by removing cases or running a cheaper model that also degrades quality, moving cost without honestly improving efficiency. Counter: Pair with gating-pass-rate and scored-case count so a cost drop that comes with quality loss or case removal is visible. |
| failure_modes | Proxy drift: the fallback token-price computation diverges from actual billed cost if the price table lags a model change.; Stale source: no run means no fresh cost; last value may predate a model or prompt change.; Errored cases inflate cost with no scored output, so cost can rise while useful coverage falls. |

## Leading indicators

### `days-since-last-run` · Days since last run (per subject)

| | |
| --- | --- |
| type | leading → gating-pass-rate |
| direction | lower is better |
| unit | days |
| definition | For each subject: (now) minus max(started_at) across all its rows in eval_runs, in days. Computed per subject. If a subject has never been run (no rows), report the state 'never-measured', never 0. |
| source | eval_runs (Postgres): SELECT subject, now() - max(started_at) FROM eval_runs GROUP BY subject. continuous (derived on read). Owner: Owner of the six systems (program sponsor). |
| stale_after | 1 day (this metric is about staleness itself) |
| so_what | If days-since-last-run for a subject exceeds 14, the gating-pass-rate for that subject is stale and the sponsor cannot trust a freeze/ship decision; the decision is to trigger a run before deciding, or to stop measuring that subject if it is deliberately abandoned. |
| leads | gating-pass-rate — mechanism: The store only fills when a human runs a suite; the longer since the last run, the more code_version/prompt_version drift accumulates unmeasured, so a rising gap precedes an unnoticed pass-rate regression. Lead time: days to weeks ahead of the next observed pass-rate change |
| goodhart | **low.** Gaming path: none Counter: none |
| failure_modes | Instrumentation hides the outcome: CI never writes to the store, so a subject can be exercised in CI yet look unmeasured here.; A single trivial run resets the clock without providing real coverage, so pair reading with scored-case count. |

### `advisory-degradation-rate` · Advisory characteristic degradation rate

| | |
| --- | --- |
| type | leading → gating-pass-rate |
| direction | lower is better |
| unit | % |
| definition | Comparing the two most recent runs per subject: numerator = count of advisory characteristic evaluations (advisory = true) that passed in the older run but failed in the newer run, over matched case_id + characteristic name; denominator = count of advisory evaluations present in both runs for matched case_id + name. If fewer than two runs exist or the denominator is zero, report state 'no-signal', never 0%. |
| source | eval_runs (Postgres) + record.results[]: Two latest run_ids per subject; join results[] on case_id, characteristics[] on name where advisory=true; count passed(old)&&!passed(new) over matched pairs. per run (needs two runs). Owner: Owner of the six systems (program sponsor). |
| stale_after | 21 days |
| so_what | If advisory degradation exceeds 10% of matched advisory checks between consecutive runs, the sponsor promotes the affected advisory characteristic to gating or freezes prompt changes before an advisory slip becomes a gating failure. |
| leads | gating-pass-rate — mechanism: Advisory characteristics measure softer quality on the same outputs; they slip before a gating threshold is crossed because they capture partial degradation the gate rounds to pass, giving early warning. Lead time: 1-2 runs (roughly 1-2 weeks) before a gating failure |
| goodhart | **medium.** Gaming path: Keep characteristics advisory forever so degradations never gate, or drop advisory checks so nothing is matched. Counter: Pair with count of matched advisory checks and the ratio of advisory-to-gating characteristics per subject. |
| failure_modes | Proxy drift: advisory checks that used to correlate with gating quality may stop predicting it.; Denominator collapse: if characteristic names change between runs, matched pairs shrink and the rate becomes noisy.; Survivorship: cases that errored in either run drop from matching, hiding degradation on the flakiest cases. |

### `errored-case-share` · Errored case share (per subject)

| | |
| --- | --- |
| type | leading → gating-pass-rate |
| direction | lower is better |
| unit | % |
| definition | For the most recent run per subject in the trailing 14-day window: numerator = count of results[] where error is not null; denominator = count of all results[] in that run. If the denominator is zero (run had no cases), report state 'no-signal', never 0%. |
| source | eval_runs (Postgres) + record.results[]: Latest run_id per subject within 14d; count results[] where error IS NOT NULL over count(results[]). per run. Owner: Owner of the six systems (program sponsor). |
| stale_after | 21 days |
| so_what | If errored-case share for a subject exceeds 15%, the sponsor treats the run as untrustworthy (its pass rate is over a shrunken base) and either changes the model/harness or freezes decisions on that subject until errors are fixed. |
| leads | gating-pass-rate — mechanism: Errored cases are excluded from scoring, so a rising error share means the pass rate is computed over fewer, likely-easier cases; error growth precedes an artificially inflated or unreliable pass rate. Lead time: same run to 1 run ahead |
| goodhart | **medium.** Gaming path: Retire or exclude the cases that keep erroring so the share falls without fixing the underlying failure. Counter: Pair with total scored-case count and days-since-last-run so error removal via case retirement is visible. |
| failure_modes | Denominator collapse: retiring flaky cases lowers the share while masking real instability.; Instrumentation hides the outcome: an errored case produces nothing to score, so high error share silently biases the pass-rate numerator upward.; Stale source: reflects only the last human-triggered run. |

### `unmeasured-code-versions` · Unmeasured code-version drift (per subject)

| | |
| --- | --- |
| type | leading → gating-pass-rate |
| direction | lower is better |
| unit | count |
| definition | For each subject: count of distinct consumer package versions (tags) released in the subject's GitHub repo that are newer than the code_version of that subject's most recent eval_runs row. Compare release tags to the latest recorded code_version. If the subject has no eval_runs row, report state 'never-measured'; if tag/version formats cannot be compared, report state 'unavailable', never 0. |
| source | GitHub repositories + eval_runs (Postgres): Per subject: latest eval_runs.code_version vs GitHub release tags; count tags with version > recorded code_version. per release / on read. Owner: Owner of the six systems (program sponsor). |
| stale_after | 7 days |
| so_what | If two or more shipped versions of a subject have gone unmeasured, the sponsor triggers a run against the latest version before trusting any freeze/ship decision, or accepts the risk explicitly and cuts scope. |
| leads | gating-pass-rate — mechanism: Each shipped version can introduce a regression; versions released after the last measured one carry unverified risk, so accumulating unmeasured versions precede the pass-rate becoming untrustworthy for the version actually in production. Lead time: days to weeks before a regression would surface in a run |
| goodhart | **low.** Gaming path: none Counter: none |
| failure_modes | Proxy drift: code_version is the consumer package version, not a commit, so tag-to-version mapping can be ambiguous and miscount.; Survivorship: only released/tagged versions count; unreleased-but-shipped changes are invisible.; No branch/PR record exists, so a version measured on a side branch cannot be distinguished from the shipped one. |

## Rejected and proxied candidates

| Candidate | Ground | Reason |
| --- | --- | --- |
| Number of runs executed per week | activity | A pure count of things the team did; doubling it by re-running suites needlessly would not please the sponsor and does not measure output quality. Days-since-last-run captures the useful coverage signal without rewarding busywork. |
| Total cases in the suite | diagnostic | Useful context in a drill-through and as a counter-metric for pass rate, but on its own it changes no sponsor decision at SVP altitude; it belongs beside the outcome, not at the root. |
| Regressions caught before production | unmeasurable | The brief states there is no record of regressions discovered by any means other than a run, and no branch/PR linkage, so 'caught before production' cannot be sourced honestly. **Proxy:** Advisory-degradation-rate as an early-warning signal of quality slips **Misses:** It only sees regressions that a run happened to exercise on advisory characteristics; regressions in unmeasured versions or gating-only failures never appear as advisory degradation. |
| Mean latency per case | diagnostic | latency_ms is recorded, but the sponsor's stated decisions are freeze prompts, change model, add/retire cases, stop measuring eeds none of these directly; cost already captures the spend decision. Latency is drill-through detail. |
| Overall pass rate across all subjects combined | duplicate | Aggregating gating pass rate across six heterogeneous subjects hides per-subject regressions (the unit at which freeze/model decisions are made) and moves together with per-subject rates; it is one distraction on top of gating-pass-rate. |

## Notes

- Both outcomes are reported per subject rather than as a single program-wide number because every sponsor decision (freeze, change model, retire cases, stop measuring) is made at the subject level; the tree stays within one/two outcome KPIs by treating each as a per-subject metric family, not by adding roots.
- Rubric test 1 forced 'runs executed' to be rejected as activity even though it is the most natural throughput number; days-since-last-run is the honest coverage/staleness signal instead.
- The store is silent unless a human runs a suite and CI discards results, so staleness is a genuine first-class risk here; days-since-last-run and unmeasured-code-versions exist mainly to expose when the outcome KPIs are stale rather than good.
- Errored cases are excluded from pass-rate scoring per the brief's definition of error ('produced nothing to score'), which creates a survivorship bias; errored-case-share is included specifically to keep that bias visible, per test 6.

