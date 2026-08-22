# Review — Observability Platform GA: baseline vs agent draft

Baseline: [`simulated-program.md`](simulated-program.md), hand-written first.
Draft: [`simulated-program.agent.md`](simulated-program.agent.md), rubric v1,
define prompt v1, `claude-opus-4-8`, one call (7,676 in / 5,277 out tokens,
≈ $0.17). The draft passed the shape checks on the first attempt.

The rubric's review procedure: outcomes must agree in substance; every
leading-indicator disagreement gets a recorded reason; a disagreement that is
really a rubric gap amends the rubric.

## Outcomes — agree

| Baseline | Agent | Verdict |
| --- | --- | --- |
| `forecast-slip-days` | `forecast-slip-days` | **Same thing measured.** Same formula shape (remaining points ÷ trailing throughput vs committed GA). The agent's trailing window is 28 days; the baseline's is 14. Keep **14**: 28 days is 40 % of a ten-week program and would not show the week-5 slip until week 7–8, which the ledger (RC1-300) needs seen within 14 days. The agent's wording for a zero-throughput denominator — *report broken with reason "no completed work to forecast from"* — is better than the baseline's "undefined" and is **adopted**. |
| `cost-vs-envelope` | `cost-envelope-variance` | **Same thing measured.** Cumulative actual − cumulative plan. The agent reports USD only and moves the single-week view to its own leading indicator (below); the baseline folded the single week into the outcome. The agent's arrangement is cleaner and is **adopted** — see L4. The baseline's *% of plan-to-date* stays as the second unit; the agent's threshold (10 % of the whole envelope) and the baseline's (110 % of plan-to-date for two weeks) trip in roughly the same place and the baseline's is kept because it is defined at every week, not only at the end. Owner note adopted from the agent: *TPM; Finance owns the source row.* |

Sponsor question as restated by the agent — *"will we hit the date, stay
inside the envelope, and find out early enough to act"* — is the brief's
sentence, correctly read. It also explains the agent's one structural
choice: it treated "find out early" as the job of the leading indicators
rather than a third outcome, which is right.

## Leading indicators — every disagreement, with the reason

### Agent proposed, baseline did not

**`throughput-vs-required` (activity-derived) — rejected: duplicate of O1.**
The agent defines it as trailing throughput minus *required* run-rate
(remaining points ÷ weeks remaining), and claims a 2–3 week lead over the
forecast. It has none: `slip > 0 ⇔ throughput < remaining ÷ weeks_remaining`,
so the gap is negative exactly when the forecast is late. Same inputs, same
day, different units. The rubric's *duplicate* ground applies — two numbers
that always move together are one KPI. The agent's failure-mode list for it
is good (required rate spikes as weeks remaining shrinks) and is the reason
the baseline's own throughput indicator compared against the *plan* rate,
which does not have that denominator. This is a rubric gap — see amendments.

**`reopen-rate` — kept as O1's counter-metric, not given a slot.** The
baseline already pairs it with the forecast as the Goodhart counter; the
agent promoted it to a leading indicator with a real decision attached
(freeze the Done definition, add a review gate). The decision is good and
is adopted into O1's `goodhart.counter` text. But a counter-metric is
reported beside its outcome in every brief regardless; giving it a leading
slot costs the tree one of four and buys nothing. Resolved the same way in
the eval-store review, which makes it a rule — see amendments.

**`weekly-spend-burn-ratio` — adopted as L4.** Single-week actual ÷ plan,
leading the cumulative variance by construction (the cumulative figure is the
running sum of weekly gaps, so the weekly ratio crosses first). The baseline
had this buried inside O2's definition. The agent's threshold (1.2 for two
consecutive weeks) is adopted; the baseline's single-week 150 % escalation
stays as the rule the week-6 planted spike must trip.

**`blocked-critical-path-points` — merged into the baseline's L3.** The agent
counts absolute blocked points restricted to the tracing → SLO → alerting
chain; the baseline counts blocked share of *all* open points, direct and
transitive. The agent's restriction to the critical path is the better
scope — a blocked stretch story off the chain is not the sponsor's problem —
and is **adopted**. The agent's absolute-count unit is not: a threshold of
"8 points" has no meaning against a program whose size the sponsor does not
carry in their head, and the percentage form survives re-estimation.
Resolution: L3 = blocked share (%) of open points **on chains to GA-blocking
stories**, direct and transitive halves reported separately.

### Baseline proposed, agent did not

**`scope-change-pct` (L1) — kept. The agent's largest miss.** The draft has
no scope indicator at all; it mentions silent de-scoping only as a
failure mode of the forecast. But scope added in week 3 with seven weeks
left is a different decision from the same add with two weeks left, and
the forecast alone cannot tell the sponsor *why* it moved — scope or pace.
The week-3 planted event is the first thing the ledger tests, and the
agent's tree would see it only as a forecast move with no attribution.
The rubric asked for a mechanism and a lead time; the brief gave the
created-date field. The agent had what it needed and did not use it.

**`critical-path-slack-days` (L2) — kept.** The agent's blocked-points
indicator is status-based: it moves when a story is marked Blocked or its
upstream is not Done. An upstream *due date* moving fourteen days later —
the week-5 planted event — changes no status on the day it happens. Only a
date-based slack calculation sees it on day one; the agent's tree sees it
one to two weeks later when the downstream fails to start. The platform
already ships this rule (`drift/rules.py`, timeline inversion), which is
the other reason to keep it: verified source.

**`throughput-points-per-week` (L4 in the baseline) — moved to the
drill-through.** Adopting the weekly burn ratio makes five leading
indicators; the rubric allows four. Throughput is the denominator of O1 and
is reported inside O1's detail anyway; the scope-vs-pace attribution the
baseline wanted from it is answered by L1 (if scope did not move and the
forecast did, it was pace). It goes, labelled activity-derived, to the O1
drill-through. The agent had it at the root of its leading set, which is
consistent with its missing the scope indicator — it was using throughput
to do a job scope change does better.

## Rejected candidates — compared

| Candidate | Baseline | Agent | Note |
| --- | --- | --- | --- |
| Stories closed / velocity | activity | activity | Agree. |
| Comments / engagement | no-decision | activity | Agree on the verdict; the baseline's ground is the sharper one — it is activity *and* changes nothing. |
| Trace coverage (% services emitting traces) | — | unmeasurable, proxy offered then withdrawn | The agent raised the truest product outcome and rejected it honestly: no source in the brief. The baseline's equivalent was *on-call alert volume after GA*. **Adopted into the rejected set** as the successor-program note. |
| SLO breach / alert response | — | unmeasurable | Same as above. |
| % stories with dates populated | — | diagnostic | Agree; it is the forecast's data-hygiene check. Adopted as a drill-through item. |
| Open drift findings, days since update | diagnostic / no-decision | — | The agent did not consider them; no disagreement. |

## Rubric amendments proposed (for v2)

Not applied yet — the trees under review were drafted under v1, and the
version bumps when the instrument stage (RC1-303) has exercised the adopted
set. Recorded here so the reason travels with the change.

1. **Lead time must be demonstrable, not asserted.** A leading indicator
   computed from the same inputs as its outcome with no time offset is a
   duplicate, whatever the mechanism text says. Test: could the outcome move
   while this did not, on the same snapshot? If not, it is the outcome.
2. **Counter-metrics do not take a slot.** A Goodhart counter is reported
   beside its outcome in every brief by rule; it is not also a leading
   indicator unless it predicts something the outcome's own movement does
   not.
3. **Name the first-day detector for each known risk.** When a program
   brief names a category of risk (scope, dates, cost), the tree should say
   which indicator sees it on the day it happens. The agent's draft would
   have seen the two schedule risks only after they had become forecast
   slip — a tree that only confirms is a lagging tree.

## Adopted tree (what RC1-303 instruments)

| id | type | from |
| --- | --- | --- |
| `forecast-slip-days` | outcome | baseline, with the agent's broken-with-reason wording and reopen-rate decision |
| `cost-vs-envelope` | outcome | baseline, single-week view moved to L4 |
| `scope-change-pct` | leading → forecast | baseline |
| `critical-path-slack-days` | leading → forecast | baseline |
| `blocked-share-pct` | leading → forecast | baseline, restricted to GA-blocking chains per the agent |
| `weekly-spend-burn-ratio` | leading → cost | agent |

Drill-through, not KPIs: throughput (activity-derived), reopen rate (O1
counter), unestimated open stories (L1 counter), stories missing dates.

**Outcome:** outcomes agreed; of the agent's four leading indicators one
adopted, one merged, one rejected as a duplicate, one kept as a counter; of
the baseline's four, three kept (one narrowed) and one demoted. The ledger
(RC1-300) is derived from the adopted set, not the baseline.
