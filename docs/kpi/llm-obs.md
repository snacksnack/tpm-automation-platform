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
