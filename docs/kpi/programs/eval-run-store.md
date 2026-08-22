# Program brief — Agent Eval Harness (the real program)

This is the input the define stage receives for the real program. It describes
the program and its data sources; it does not name KPIs.

## The program

`agent-evals` is a regression suite for the LLM systems already shipped across
six repositories (a PR review agent, a dependency-drift digest, a stakeholder
status email, a launch planner with several subjects, a concert-preview
writer, an incident summarizer). Each system is a **subject**; each subject
runs against frozen **cases**; each case is scored on named **characteristics**
that are either **gating** (can fail a build) or **advisory** (reported,
never gating). Every run is recorded with the subject's code version, the
model, the prompt version, and the exact token cost and latency of every
case.

The program exists so that the question *"how do you know the output is any
good?"* has a measured answer for every shipped system — and so that the cost
of having that answer is known.

- **Sponsor:** the owner of the six systems, acting as their own SVP: the
  decisions on the table are *freeze prompt changes on a repo*, *change the
  model a subject runs on*, *add or retire cases*, and *stop measuring a
  subject*.
- **Duration:** ongoing. The tree is tracked weekly for at least three
  consecutive weeks.
- **How runs happen:** a human runs a suite from the consumer repo and then
  publishes the trend page. **CI never writes to the store** — it runs only
  the free, deterministic subjects and discards the result. So the store fills
  when someone chooses to take a measurement and is silent otherwise.
- **Billing:** model spend is per token at published per-model prices; the
  store itself is a fixed-price hosted Postgres plan.

## Data sources the program controls

| Source | What it holds | Cadence |
| --- | --- | --- |
| `eval_runs` table (Postgres, append-only) | one row per run: `run_id`, `subject`, `code_version`, `model`, `prompt_version`, `started_at`, and a `record` JSON document | written whenever a suite is run |
| `record.results[]` inside each row | per case: `case_id`, `error` (null or a message — an errored case produced nothing to score), `usage.input_tokens`, `usage.output_tokens`, `usage.cost_usd`, `usage.latency_ms`, `characteristics[]` each with `name`, `passed`, `advisory`, `detail`, and `observations` (free-form structured facts) | same |
| Model price table | input and output price per million tokens per model id | static; edited when a model is added |
| Postgres plan billing | fixed monthly price for the store's hosted plan | monthly |
| GitHub repositories of the six subjects | commits, tags, pull requests; the subject's `code_version` is the consumer package version, not a commit | live |
| Trend page | a rendered view of the store; derived, not a source | republished after a run |

There is no record of which branch or pull request a run was taken on, and no
record of regressions discovered by any means other than a run.

## Constraints on the tree

- One or two outcome KPIs; three or four leading indicators.
- Every KPI names its source from the table above, precisely enough to query.
- Anything the sponsor would not change a decision over is not a KPI.
