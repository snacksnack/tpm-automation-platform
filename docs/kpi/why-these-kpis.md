# What KPIs did you track for a program — and why those? (RC1-308)

The two-minute answer, for the real program the KPI agent runs against: the
**agent-evals run store** — the regression suite measuring six shipped LLM
systems, tracked daily, narrated weekly, escalated when unmeasurable.

## The answer

The sponsor question is: *for every shipped LLM system, is the output
measurably good right now, and what does knowing that cost?* Two outcomes,
because that question has exactly two halves:

- **`gated-pass-rate`** — per subject, the latest run's passing share of
  scorable cases; program-level, the *minimum* across billed subjects. The
  minimum, not the mean: one bad system is a bad answer to "is the output
  good", no matter how the other five average out. Its so-what is a decision
  the sponsor pre-agreed: two consecutive readings under 80 % freeze prompt
  changes on that repo.
- **`real-cost-per-run`** — billed dollars (the org's Anthropic cost report
  plus the store's Heroku invoice, prorated) over the runs actually taken.
  Until RC1-308 the cost half was a *construction* — model spend from a
  price table, the store from a declared $5 constant. Now the numerator is
  what was billed, and the KPI trips when real spend runs 3x past what the
  price table can attribute — the gap is money the measurement program
  cannot account for.

Four leading indicators, each with a stated mechanism into an outcome:

- **`measurement-freshness-days`** — a regression in a subject nobody has
  run *cannot appear* in the pass rate; staleness is where a wrong pass
  rate comes from before it is wrong.
- **`error-rate`** — errored cases shrink the scorable base; a pass rate
  over a collapsed denominator moves before it lies.
- **`cost-per-run-by-model`** — model mix is the lever that moves the real
  bill (tripped in the very first brief: one subject at 3.8x the cheapest
  model that runs it).
- **`cost-per-verified-case`** — the attributed unit cost, demoted from
  outcome to leading by the RC1-308 amendment: attribution is the
  *controllable share* of the bill, and it moves first.

What was refused matters as much: **runs per week** (activity — counts how
often a human chose to measure, not whether systems are good), **time to
green** (duplicate of the caught-and-fixed proxy), **regressions caught
before merge** (the outcome the program exists for — and unmeasurable,
because the store carries no branch and CI never writes; kept as a proxy
with its misses stated), and **unmeasured code versions** (adopted, then
rejected by the instrument stage: released-but-never-run versions are not
visible from any source the snapshots hold — so the tree was amended to say
so instead of apologizing weekly for a KPI that cannot ship).

Every number is enforceably honest: a KPI whose source is gone reads
`broken` with the last good value *and its date*, never zero; the weekly
brief is written by a model that is refused if its prose contains a number
the readings do not.

## The contrasting case: the job-search agent (RC1-91)

The job-search agent is the same lesson from the other side: **an outcome
hidden by instrumentation.**

The pipeline is real — deterministic ATS ingestion across a watchlist,
a local board with a status lifecycle, cached LLM skill-match scores,
tailored materials. Its sponsor question is "am I converting effort into
interviews and offers?" — a funnel. And the funnel *cannot be computed*:
the interview stage was never recorded as a dated transition, and
applications carry no apply date. The states exist on the board; the
*when* was never written down. So conversion ("applied → interview, and
how long") — the outcome the whole machine exists for — is unmeasurable
from the data the machine keeps, exactly the way "regressions caught
before merge" is unmeasurable from a store that CI never writes to.

The eval-run store answered that problem before it happened: every run is
append-only with a `started_at`, so freshness, trend and cost are queries.
The job-search agent shows what the alternative looks like — a rich
pipeline whose outcome KPI would have cost one timestamp column at design
time, and instead costs a backfill that can never be complete. That is why
the KPI lifecycle here runs **define → instrument → track**: the
instrument stage exists to catch "the tree names an outcome the sources
cannot carry" *before* three weeks of tracking, not after.

## Where to verify any of this

| Claim | Where |
| --- | --- |
| The tree, amendments, rejections | `docs/kpi/trees/eval-run-store.adopted.json`, `.review.md` |
| What ships and why | `docs/kpi/instruments/eval-run-store.json` |
| The readings, daily | `kpi_readings` (Postgres), Grafana `eval-run-store` |
| The weekly briefs, traceable to snapshots | `kpi_briefs` → `run_id` → `python -m collectors show eval-run-store --run N` |
