# Review — Agent Eval Harness: baseline vs agent draft

Baseline: [`eval-run-store.md`](eval-run-store.md), hand-written first.
Draft: [`eval-run-store.agent.md`](eval-run-store.agent.md), rubric v1, define
prompt v1, `claude-opus-4-8`, one call (7,942 in / 5,071 out tokens,
≈ $0.17). Passed the shape checks on the first attempt.

## Outcomes — agree on what, disagree on units

| Baseline | Agent | Verdict |
| --- | --- | --- |
| `gated-pass-rate` | `gating-pass-rate` | **Same thing measured**, per subject, gating characteristics only, errored cases excluded from the base. Two differences. (1) The agent counts characteristic evaluations; the baseline counts cases (a case passes when every gating characteristic does). Keep **case-weighted**: it is `CaseResult.passed` in the harness and what the trend page plots, so the KPI and the page cannot disagree. (2) The agent's decision threshold — freeze the repo on *any* gating failure or a 5-point drop — would freeze seven of sixteen subjects on today's data. A threshold that trips on day one is a finding about the program, not a decision rule. Keep the baseline's **80 % on two consecutive measurements**. The agent's `no-signal` state for a 0/0 base is adopted as the name for what the baseline called *broken* on this KPI. |
| `cost-per-verified-case` (+ $ per sweep) | `cost-per-run` (per subject) | **Same concern — the cost of knowing — different denominator, and the difference matters.** The agent's number is per run per subject, model spend only. That is the baseline's *L4*. Two things it leaves out: the **fixed store cost**, which at this month's volume ($5 plan vs $4.69 of model spend) is the larger half of what the program costs and is the honest line for a sponsor — *"the database costs more than the models until you run more"*; and **normalisation by cases scored**, without which a subject that adds cases looks more expensive for doing its job. Keep the baseline's outcome; the agent's becomes L4, where the baseline had it. The agent's fallback — recompute from tokens × price when `cost_usd` is absent — is **adopted**, with one change: the recomputed value is reported *and* the missing field is flagged as a broken instrument, because the runbook says a $0 billed run is a consumer bug, and a silent fallback would hide it. |

## Leading indicators — every disagreement, with the reason

### Both proposed

**`measurement-freshness-days` / `days-since-last-run` — agree.** Same
definition. Thresholds differ (7 vs 14 days before the subject's pass rate
is distrusted). Keep **7**: the brief says the tree is tracked weekly; a
subject unmeasured for a whole reporting period is the thing the brief is
for. The agent's failure mode — *a single trivial run resets the clock* — is
the baseline's Goodhart note, and both point at case count as the counter.

**`error-rate` / `errored-case-share` — agree.** Same definition, same
mechanism (errors shrink and flatter the pass-rate base; the fix is
infrastructure, not prompts). Thresholds 20 % vs 15 %; nothing in three days
of data distinguishes them — `stakeholder-status-email` is at 25 % either
way. Keep 20 % and revisit after three tracked weeks.

### Agent proposed, baseline did not

**`unmeasured-code-versions` — adopted, pending verification. The agent's
best contribution.** Count of consumer package versions released after the
`code_version` of the subject's last run. It is a sharper statement of the
risk than days-since-last-run: a subject unmeasured for 14 days with no
release is fine; one with two releases is carrying unverified changes in
production. The baseline did not think of it. It is adopted into the tree
**conditionally**: the brief lists GitHub as a source, but whether the six
consumer repos tag releases consistently is exactly the question the
instrument stage (RC1-303) exists to answer. If tags are not there, the
honest proxy is commits to `main` since the commit that set the measured
version, and the caveat is that a version bump without a tag is invisible.
If neither is verifiable, it is rejected there with that reason.

**`advisory-degradation-rate` — not adopted; drill-through hypothesis.**
Advisory characteristics failing between consecutive runs as an early
warning before gating ones fail. The mechanism is plausible (softer checks
on the same output slip first) but it is a hypothesis, not a mechanism the
data has shown: it needs two runs per subject matched on `case_id` and
characteristic name, and the advisory set is small (the drift digest has one
advisory characteristic in seven). Test 2 asks for a mechanism; this one is
"should correlate". It stays in the drill-through; if three weeks of tracked
data show an advisory slip preceding a gating failure even once, it is
promoted and the rubric gets the evidence.

### Baseline proposed, agent did not

**`gating-coverage` (L2: gating cases per subject, advisory share) — kept
as O1's counter-metric, not a slot.** The agent did not propose it as a
KPI; it named it, correctly, as the counter for the pass rate and rejected
"total cases" as a diagnostic. That is the same resolution the simulated
review reached for reopen rate, and it is the right one: a counter travels
with its outcome by rule and does not need one of four slots. The baseline
over-promoted it.

**`cost-per-run-by-model` (L4) — kept; it is the agent's O2.** See the
outcome table. The agent's "latest run within 30 days" window and its
45-day staleness are too loose for a weekly brief; the baseline's 7 days is
kept.

## Rejected candidates — compared

| Candidate | Baseline | Agent | Note |
| --- | --- | --- | --- |
| Runs per week | activity | activity | Agree, for the same reason: it counts attention, not quality. |
| Regressions caught before merge | unmeasurable → proxied as fail-then-pass on a newer `code_version` | unmeasurable → proxied as advisory degradation | Agree on the verdict. The baseline's proxy is closer to the outcome (a fail followed by a fix is a caught regression; an advisory slip is a hint). Keep the baseline's proxy with its caveat; the agent's becomes the drill-through hypothesis above. |
| Total cases in the suite | (counter inside L2) | diagnostic | Agree — it is the counter. |
| Mean latency per case | — | diagnostic | The agent considered it and correctly found no sponsor decision on it. Adopted into the rejected set. |
| Overall pass rate across all subjects | (baseline uses the minimum, not the mean, for this reason) | duplicate — hides the failing repo | Agree, and the agent's reasoning is the argument for the baseline's minimum. |
| Hallucination rate, judge agreement, trend-page freshness | diagnostic / unmeasurable / no-decision | — | Not considered by the agent; no disagreement. |

## Rubric amendments proposed (for v2)

Same list as the simulated review, plus one from this program:

4. **Calibrate thresholds against the current reading when one exists.**
   A so-what threshold that the program already breaches on the day the tree
   is drafted is not a decision rule; it is a finding, and the brief's first
   job is to report it. The agent's "freeze on any gating failure" would
   have frozen seven repos on day one.

## Adopted tree (what RC1-303 instruments, then RC1-305 tracks)

| id | type | from |
| --- | --- | --- |
| `gated-pass-rate` | outcome | baseline, case-weighted, with the agent's `no-signal` state |
| `cost-per-verified-case` (+ $ per sweep) | outcome | baseline, with the agent's token×price recomputation as a flagged fallback |
| `measurement-freshness-days` | leading → pass rate | both |
| `unmeasured-code-versions` | leading → pass rate | agent — **pending source verification in RC1-303** |
| `error-rate` | leading → pass rate | both |
| `cost-per-run-by-model` | leading → cost | baseline (the agent's O2, demoted) |

Counters reported beside the outcomes: gating case count and advisory share
(for pass rate); cases scored and sweep cost (for unit cost). Drill-through:
advisory degradation (hypothesis), caught-and-fixed proxy, latency.

**Outcome:** outcomes agreed in substance; the agent's cost outcome was the
baseline's leading indicator and is kept there. Of the agent's four leading
indicators two matched the baseline, one is adopted pending verification,
one is parked as a hypothesis. The baseline lost one slot to a counter it
had over-promoted — the same correction the agent's draft implied. One real
miss on the baseline's side (`unmeasured-code-versions`) and one on the
agent's (the fixed cost and the unit denominator).

## Amendment — real billing feeds (RC1-308, 2026-08-25)

Amended by hand when the real cost sources landed; the shape rules were
re-validated, not waived.

- **`real-cost-per-run` added as the economics outcome.** Both prior cost
  numbers were constructions — model spend from a price table, the store
  from a declared constant. The new outcome is billed dollars only: the
  org's Anthropic cost report plus the Heroku invoice, prorated, over the
  runs actually taken. The org feed cannot be filtered to eval traffic, so
  the KPI carries that as a stated upper-bound caveat and trips on the gap
  (real over 3x attributed, two consecutive readings) rather than
  pretending precision it does not have.
- **`cost-per-verified-case` demoted to leading.** It keeps shipping
  unchanged — same measure, same history — but it is the *attribution*,
  and attribution now leads the bill instead of standing in for it.
- **`unmeasured-code-versions` moved to rejected (unmeasurable).** The
  instrument stage's verdict (RC1-303) said so from the start; the tree now
  agrees instead of carrying a KPI that can never ship and a weekly "not in
  this brief" apology for it.
- **`cost-per-run-by-model` retargeted** to lead the new outcome — model
  mix is the lever that moves the real bill.
