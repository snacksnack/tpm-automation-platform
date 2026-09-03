# LLM Observability (RC1-322)

The platform's billed stages ship traces to Datadog LLM Observability —
every Anthropic call as an LLM span with model, tokens, latency and
estimated cost, grouped so the product's app list reads as the fleet
inventory:

| ml_app | what it covers | enabled where |
| --- | --- | --- |
| `kpi-agent` | `kpi.define`, `kpi.instrument`, `kpi.narrate` (service = the stage) | each CLI's `main()`, via `observability.enable_llm_obs` |
| `drift-digest` | the drift service's digest calls (`/drift/run`) | `main.py` at startup |
| `tpm-platform` | `python -m evals` billed subjects, service `evals` | `evals/__main__.py`, via `agent_evals.llmobs` |

Two helpers on purpose. `observability.enable_llm_obs` is a py-module beside
`config` because the drift service bills in production and agent-evals never
ships in the image (RC1-265). The evals CLI is dev-only by design, so it uses
`agent_evals.llmobs` (v0.4.0), which adds what only a harness needs: a
workflow span per case, with the harness verdict (categorical) and cost
(score) submitted as Datadog *evaluations* — they render in the same column
as Datadog's built-in evals.

## Switching it on and off

One switch: `DD_API_KEY`. Present, traces flow; absent, every enable call is
a no-op and nothing else changes — same one-home rule as the metrics leg
(`~/.zshrc`, pulled by `scripts/kpi_daily.sh` and `scripts/kpi_weekly.sh`).
`DD_SITE` defaults to `datadoghq.com`. Agentless: spans post straight to the
intake, no local Datadog agent daemon.

The deployed drift service needs `DD_API_KEY` set as a Fly secret before its
traces appear; until then it runs untraced, by the same no-op rule.

## Cost

Datadog bills LLM Observability on LLM spans only (workflow/tool/eval spans
are free) with 40k LLM spans/month free at 15-day retention. The whole
estate makes low hundreds of model calls a month — margin is ~two orders of
magnitude, so this stays $0. Traces capture full prompt and response text;
all of it is our own generated content.

## The parse gap (why agent-evals patches `Messages.parse`)

ddtrace's Anthropic integration wraps only `messages.create`/`stream`.
`messages.parse` posts directly (verified anthropic 1.2.0 / ddtrace 4.14.0),
so structured-output callers — the agent-evals judge, every launch-planner
agent — would silently produce no spans. `agent_evals.llmobs.enable()`
patches `parse` with an equivalent llm span. This platform's own stages all
use `create` and need no patch.

## Watching the fleet's spend (RC1-349, RC1-377)

Two hand-created monitors, both tagged `fleet:llm-obs` / `service:agent-fleet`,
both routed into the incident summarizer (RC1-375), both with
`notify_no_data` off — a quiet fleet is not a failure. Neither is generated:
they watch integration metrics, not anything the instrument stage rewrites.

**Daily spend guardrail — 318097614.** `sum(last_1d)` of
`ml_obs.span.llm.total.cost` / 1e9 (the metric is nanodollars). Created by
RC1-349 at warn $1.50 / alert $3, about 15× the ~$0.20/day the fleet cost
before the PR review agent started reviewing every merge. That line lasted
three days: the daily total tracks how many PRs merge, and 2026-08-30 →
09-02 read $2.65 · $2.92 · $1.03 · $5.56 (a dozen reviews at $0.30–0.45
each). Once RC1-375 made an Alert an incident, a threshold that fires on
shipping days manufactured incidents. RC1-377 (2026-09-03) moved it to
**warn $6 / alert $12** — runaway territory: ten uncached reviews or a loop
cross $12 inside an hour or two, and no legitimate day has come near it.

**Cost per call — 318833109.** The signal the daily total cannot see. A
runaway loop, a broken prompt-cache prefix (RC1-350: an uncached review is
~$0.06/call against ~$0.015 cached), a model swap or prompt growth all raise
the price of a call; a busy day does not. Query:

```
sum(last_4h): cost / clamp_min(llm-span count, 10) / 1e9  >  0.05
```

Thresholds **warn $0.035 / alert $0.05** are 2× and 3× the trailing-7-day
mean of $0.017/call measured on 2026-09-03; re-derive them when the fleet's
model mix changes. `clamp_min(…, 10)` floors the denominator at ten calls so
one expensive call cannot trip it on its own — the KPI agent's own opus
define / instrument / narrate calls run $0.08–0.12 each, two or three at a
time, and are the one known benign trigger at the warn line. Datadog
validated the formula-with-`clamp_min` shape as a monitor query; the
`ml_obs.span{span_kind:llm}` count is the same series the fleet dashboard's
"avg cost per model call" tile divides by.

**Rejected: anomaly and forecast monitors.** Two weeks of history is not
enough for a seasonal model, and the driver — how much shipped that day —
has no weekly shape to learn. Per-app caps were rejected too: one app is
90 % of spend, so a cap on it is the fleet cap under another name.

Triage for either alert is on the fleet dashboard (`bwm-uny-qqs`): the
caching row says whether the prefix broke, cost by model says what ran on
what, cost by ml_app names the spender.
