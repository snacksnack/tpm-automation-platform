# Test health: the SLO and the scorecard rules (RC1-467)

RC1-452 made all nine estate repos (plus job-search-agent) report test-level
results to Test Optimization; RC1-468 added `code_coverage.lines_pct`. This
story is the consumption layer: one fleet SLO and two scorecard rules, so the
data answers questions instead of sitting in an explorer.

## The mechanism: counts we post, because events cannot back an SLO

Test Optimization stores events. No `ci.test.*` metric materializes, a
`ci-tests alert` monitor is the same class as the `ci-pipelines alert`
monitor that RC1-456 proved cannot back an SLO, and the generate-metrics
config surface is not exposed to this account (probed 404, both spellings).
So `kpi/test_health.py` does what the drift heartbeat and the RC1-359
scanner gauges already do: reads the API once a day (a step in
`scorecard-daily.yml`, which holds both DD keys), decides in Python, posts
counts:

| metric | meaning |
|---|---|
| `delivery.tests.attempted{test_service}` | pass + fail on `main`, last 24 h |
| `delivery.tests.passed{test_service}` | pass only |

Skips sit in neither number: a skip is a decision, not an outcome, and
counting it either way moves the ratio without a test having run. A service
with no events posts nothing — a quiet day adds no denominator. Count type
(`type: 1`), daily interval; ~8–22 series present one hour in twenty-four,
a fraction of one custom metric a month.

## The SLO: Fleet test health, 99% / 30d — a floor, not the baseline

`0411019e350952e09bc9c78b2eb92646`, metric type:
`sum:delivery.tests.passed{*} / sum:delivery.tests.attempted{*}`.

Measured baseline at creation (3 days of default-branch data, 09-25 → 09-28):
**7,412 pass / 3 skip / 0 fail — 100%**. A 100% baseline cannot be a target
(zero error budget measures luck, the RC1-456 denominator lesson), so 99% is
the deliberate floor: at ~74k events/30d it budgets ~740 failing test events
a month, roughly one bad fleet-wide day. **Ratchet it from the first full
30-day window** — the SLO description carries the same note.

## The scorecard rules: joined by `repo:`, honest about ambiguity

`Reports test results` and `Reports code coverage` (RULES rows 9 and 10)
resolve entity → repo through the `repo:` tag, which handles the not-1:1
mapping without a lookup table: both platform entities inherit
`tpm-automation-platform`'s reporting, `incident-summarizer` reads
`ai-incident-summarizer`. Window: 14 days, any branch — the summarizer's CI
is PR-only, so a default-branch filter would fail it forever for a wiring
choice, not a gap.

Day-one score: **both rules 8/8 on merit** (live `show`, 2026-09-28; fleet
coverage averages 70.6–100% per service). The scorecard grows 64 → 80
outcomes, 75 passing; the five standing reds are untouched.

The `Reports test results` fail remark deliberately names both possible
causes — quiet CI and broken reporting look identical in the events store,
and a remark asserting the wrong one costs the reader the first hour.

The coverage rule checks **presence, not a band**. The observed per-service
baselines run 64–100% with by-design outliers (reid_basic's resume-sync
guardrail session ~28%, n8n billed runners at 0% inside their figures), so a
single fleet threshold would either never fire or ship dishonest reds. The
banded rule waits for per-service baselines over a longer window; the pass
remark carries the measured average so the band can be set from remark
history when the time comes.

## The convergence decision: the two Has-an-SLO reds stand

The fleet SLO carries **no `service:` tag**, on purpose. `Has an SLO` asks
whether someone watches *that service's* behavior; this SLO watches the
fleet's repos, pre-production, and tagging it eight ways would flip
launch-planner-agent and stale-ticket-bot green without anyone watching
either service run. Repo-level test health is real coverage of the *code*,
not of the *service* — the reds stay until each service has an objective of
its own (launch-planner: RC1-455's usage question; stale-ticket-bot:
dormant, exclusion belongs to the rule, not the SLO list).

## job-search-agent (scope item 4, closed 2026-09-26)

Two-job CI: the zero-install `unittest` job stays the gate — it is the proof
the product needs no third-party packages — and a non-gating `report-tests`
job installs pytest + ddtrace in its own environment and reports the same
184 tests. Recorded here because the shape generalizes: instrument alongside
a constraint, never through it.
