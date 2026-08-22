# KPI tree — Agent Eval Harness (real program) — hand-written baseline

Drafted by hand under [rubric v1](../rubric.md) **before** the define stage
ran, from the [program brief](../programs/eval-run-store.md). This is the
program the interview answer is about (RC1-308): real runs, real pass rates,
real dollars.

Where this document quotes a number, it is the store as read on 2026-08-22:
59 runs across 16 subjects, all taken between 2026-08-16 and 2026-08-18,
$4.69 of model spend in total. Those numbers are here to show the KPIs are
computable, not as the first week's reading — the track stage (RC1-305)
computes them, not this document.

## Shape

```
O1  Gated pass rate, per subject           quality outcome
    L1  Measurement freshness (days since last run)
    L2  Coverage: gating cases per subject, advisory share
    L3  Error rate (cases that produced nothing)
O2  Cost per verified case (USD)           unit-economics outcome
    L4  Cost per run, by model
    (L1 and L2 also bound O2: an unmeasured subject costs nothing and
     verifies nothing)
```

## Outcomes

### O1 — `gated-pass-rate` · Gated pass rate

| | |
| --- | --- |
| type | outcome · lagging |
| direction | higher is better |
| unit | % of gating cases, per subject; program-level as the minimum across billed subjects, not the mean (a mean hides the one repo that is failing) |
| definition | For the **latest run** of each subject: cases with `error` null and every non-advisory characteristic `passed` ÷ cases with `error` null. Errored cases are excluded from the denominator and reported by L3 — an outage is not a quality regression. Program-level: the minimum across subjects with a model (billed subjects); deterministic subjects are reported but do not set the minimum. |
| source | `eval_runs.record->'results'[]` → `error`, `characteristics[].passed`, `characteristics[].advisory`; latest row per `subject` by `started_at`. Cadence: per run. Owner: Reid. |
| stale_after | 7 days since the subject's last run (see L1) |
| so_what | A billed subject under **80 %** on two consecutive measurements: freeze prompt changes on that repo until a run passes. A subject that drops more than 10 points between runs with the same `prompt_version`: the model changed, and the decision is to pin it. |
| goodhart | **high.** Gaming path: delete the hard cases, or flip a failing characteristic to advisory. Counter: L2 (gating case count and advisory share) is reported beside it; a pass-rate rise with a case-count fall is flagged. |
| failure_modes | **survivorship** — only subjects someone chose to run are in the latest-run set; the minimum is over what was measured; a subject whose latest run is weeks old is carried as stale by L1, not re-read as current; **denominator collapse** — `incident-summary` has 20 scorable cases out of 46 because 26 errored, so its 41 % is on a small base (L3 says so); a subject with 2 cases (`spec-structural`) flips 50 points on one case. |
| today | minimum across billed subjects is `incident-summary` at 41 % (on 20 scorable cases); `stakeholder-status-email` 56 %; `spec-review` 75 %; the rest 80–100 %. |

### O2 — `cost-per-verified-case` · Cost per verified case

| | |
| --- | --- |
| type | outcome · lagging |
| direction | lower is better, at constant coverage |
| unit | USD per case scored (gating, non-errored) |
| definition | `(model_spend + store_fixed_cost_prorated) ÷ cases_scored` over a trailing 4-week window, where `model_spend = Σ usage.cost_usd` across all results in the window, `store_fixed_cost_prorated` is the Postgres plan's monthly price × (window days ÷ 30), and `cases_scored` counts non-errored cases. Reported also as **$ per full sweep** — the cost of running every billed subject once — because that is the number the sponsor spends. |
| source | `eval_runs.record->'results'[].usage.cost_usd` and `error`; Postgres plan price (fixed, from the billing page; re-verified monthly). Cadence: per run / monthly. Owner: Reid. |
| stale_after | 7 days (the window rolls; if no run lands in 7 days the numerator is only the fixed cost and the value is reported stale, not as "cost fell") |
| so_what | If $ per verified case rises more than **50 %** without a matching rise in cases or a deliberate model upgrade, the decision is model choice per subject (a subject on `claude-sonnet-5` at $0.20 a run that could run on `claude-haiku-4-5` at a third of that). If a full sweep exceeds a set ceiling, sweeps go fortnightly. |
| goodhart | **medium.** Gaming path: run the free subjects more often (cases up, spend flat) — cost per case falls while the billed systems go unmeasured. Counter: L1 per billed subject; and the per-sweep figure, which does not move with free runs. |
| failure_modes | a consumer that does not set `cost_usd` records $0 and the run renders as free (the runbook notes this; an exact $0 on a billed subject is a **broken** instrument, not a cheap run); the fixed cost dominates at low volume — at this week's volume the $5 plan is larger than the $4.69 of model spend, so the unit cost is mostly a function of how often anyone runs anything, which is an honest thing to say to a sponsor; price-table changes re-price history. |
| today | $4.69 model spend over 381 scorable cases (407 total, 26 errored) ≈ **$0.012 per case** before the fixed cost; a full billed sweep is ≈ $1.70. |

## Leading indicators

### L1 — `measurement-freshness-days` · Measurement freshness

| | |
| --- | --- |
| type | leading → O1 (and bounds O2) |
| direction | lower is better |
| unit | days since the last run, per billed subject; program-level as the maximum |
| definition | `today − max(started_at)` per subject, over billed subjects. |
| source | `eval_runs.started_at`, `subject`, `model is not null`. Cadence: daily read. Owner: Reid. |
| stale_after | never — this KPI is what *defines* stale for the others; it is computed from the store's timestamps and the clock, so it is always current |
| leads | O1 — mechanism: a regression in a subject that has not been run cannot appear in the pass rate; freshness is the upper bound on how old the "current" quality reading is. Lead time: the whole gap — a subject at 14 days of freshness can have been broken for 14 days. |
| so_what | Any billed subject over **7 days**: the weekly brief names it and the ask is "run it or retire it". Over 21 days: the subject is dropped from the O1 minimum and listed as *unmeasured*, which is the honest state. |
| goodhart | **low.** Gaming path: run a subject with one case to reset the clock — which is caught by L2. |
| failure_modes | this is the program's real risk and the reason the indicator exists: all 59 runs landed in a three-day window, because runs happen when a human takes a measurement and CI never writes. A tree without this indicator would report a 78 % pass rate indefinitely off three days of data. |
| today | 4–6 days for every billed subject (last runs 2026-08-16 to 2026-08-18). |

### L2 — `gating-coverage` · Coverage: gating cases per subject, advisory share

| | |
| --- | --- |
| type | leading → O1 (Goodhart counter) |
| direction | cases: higher is better; advisory share: lower is better |
| unit | count of gating cases per subject; % of characteristics marked advisory |
| definition | From the latest run per subject: number of cases with at least one non-advisory characteristic; and `advisory characteristics ÷ all characteristics`. Both reported as a delta vs the previous run of the same subject. |
| source | `eval_runs.record->'results'[].characteristics[].advisory`; latest and previous row per subject. Cadence: per run. Owner: Reid. |
| stale_after | follows O1's subject freshness |
| leads | O1 — mechanism: a case deleted or a characteristic demoted to advisory raises the pass rate *before* any quality change; a falling case count is the earliest sign a pass-rate rise is not real. Lead time: same run. |
| so_what | A subject whose gating case count fell or advisory share rose in the same run its pass rate rose: the pass-rate improvement is **not accepted** in the brief until reviewed. That is the decision — it blocks the good news. |
| goodhart | **low** (it is the counter). |
| failure_modes | subjects with very few cases (`spec-structural`: 2) make the delta meaningless — report the absolute count; a case set that grows because of duplicated cases inflates coverage. |
| today | gating case counts range from 2 (`spec-structural`) to 56 (`tool-selection`); no advisory-share baseline yet — the first tracked week sets it. |

### L3 — `error-rate` · Error rate

| | |
| --- | --- |
| type | leading → O1 |
| direction | lower is better |
| unit | % of cases with `error` not null, per subject |
| definition | From the latest run per subject: `errored_cases ÷ all_cases`. |
| source | `eval_runs.record->'results'[].error`. Cadence: per run. Owner: Reid. |
| stale_after | follows O1's subject freshness |
| leads | O1 — mechanism: an errored case is excluded from the pass-rate denominator, so a rising error rate shrinks O1's base and makes it both more volatile and more flattering (the cases that error are often the hard ones); and errors are the harness or the environment breaking, which is a different fix from a prompt regression. Lead time: same run, but the *decision* is different, which is why it is separate. |
| so_what | Error rate over **20 %** on a subject: the decision is an infrastructure fix (key, fixture, timeout) before any quality conclusion is drawn from that subject; the brief says "unmeasured", not "41 %". |
| goodhart | **low.** Gaming path: swallow exceptions so errors become failures or passes — which would show as a pass-rate change with the same case set, and is a code review matter. |
| failure_modes | a subject where *every* case errors has a pass rate of 0/0 — reported as **broken**, never 0 % or 100 %. |
| today | `incident-summary` 57 % (26 of 46); `stakeholder-status-email` 25 % (4 of 16); every other subject 0 %. |

### L4 — `cost-per-run-by-model` · Cost per run, by model

| | |
| --- | --- |
| type | leading → O2 |
| direction | lower is better at constant case count |
| unit | USD per run, grouped by `model` and subject |
| definition | `Σ usage.cost_usd` per run, averaged per (subject, model) over the trailing 4 weeks; shown with the run's case count so a cost change can be attributed to model, prompt, or case set. |
| source | `eval_runs.model`, `subject`, `record->'results'[].usage.cost_usd`. Cadence: per run. Owner: Reid. |
| stale_after | 7 days |
| leads | O2 — mechanism: the model a subject runs on sets its per-run cost; a subject moved to a more expensive model shows here on the first run, and in O2's trailing window only as the runs accumulate. Lead time: up to the window length (4 weeks). |
| so_what | A (subject, model) pair whose per-run cost is more than **3×** the cheapest model that has passed the same subject at the same rate: the decision is to move it. `tool-selection` has runs on both `claude-haiku-4-5` and `claude-sonnet-5` and is the first place to look. |
| goodhart | **low.** |
| failure_modes | `model` is null on deterministic subjects (reported as "no model", excluded); the price table is maintained by hand and a missing price raises at record time — that is a broken instrument, not a free run. |
| today | per-run cost ranges from ≈ $0.001 (`incident-summary`, sonnet-4-6) to ≈ $0.31 (`work-breakdown`, sonnet-5). |

## Rejected and proxied candidates

| Candidate | Verdict | Reason |
| --- | --- | --- |
| Runs per week | **rejected** — activity | Counts how often a human chose to measure, not whether the systems are good. Its information lives in L1 as a freshness gap, which is the form a sponsor can act on. |
| Regressions caught before merge | **proxied**, with caveat | The outcome the program exists for, and not measurable as stated: the store carries no branch or PR, and CI never writes. Proxy: *a run with a failed gating characteristic followed within 7 days by a passing run of the same subject with a newer `code_version`* — "caught and fixed". **What it misses:** regressions nobody ran a suite on, fixes made by reverting rather than by a new version, and fixes that were prompt-only (same `code_version`). Tracked in the drill-through; promoted to the tree if RC1-303 can verify it on real history. |
| Time-to-green | **folded** into the proxy above | The same fail→pass pairing, reported as days; no separate KPI. |
| Hallucination rate | **rejected** — diagnostic | Only subjects that report `claims_checked` carry it (one today); at program level it is a per-subject characteristic, already inside O1. |
| Judge/human agreement | **rejected** — unmeasurable from the store | The harness validates its judge in code, not in run records; there is no row to compute it from. |
| Trend-page freshness (published vs store) | **rejected** — no decision | A publish step the runbook already requires; if it slips the fix is "run the script", not a sponsor decision. |

## Source break — what the tree must do

The store's credential is rotated by the host during maintenance; the runbook
already says an auth failure means "fetch the new URL", not a leak. For the
tree that means: a failed connection marks every KPI here **broken** in one
tick, carrying the last good value and date, and the escalate stage's proposed
fix is the runbook line. Nothing reads zero. This is the real program's
equivalent of the simulator's week-7 label drop, and it has already happened
at least once (RC1-263).

## The contrast case (for RC1-308)

The job-search agent (RC1-91) was examined first and rejected as the real
program. Its board reads 38 applied / 10 rejected / 0 interviewing beside 17
interview-prep packs — the outcome is hidden because the stage that would
record it is never written, and there are no apply dates to compute a cycle
time from. It is the rubric's *instrumentation hides the outcome* failure mode
as a real case, and it is why L1 and L3 are in this tree before any quality
number is.
