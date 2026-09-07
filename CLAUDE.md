# CLAUDE.md — conventions for reviewers and AI sessions

One page, under 6,000 characters so the PR review agent reads it whole. The
long story and the per-stage numbers are in the README.

## What this is

Three things sharing one codebase: a **Dependency Drift Detector** (Jira →
dependency graph → rules → Claude digest → Slack), a **Program KPI agent**
(define → instrument → track → narrate → escalate, checked against a simulated
program's ground-truth ledger), and the **Datadog account as code**.
Deterministic Python decides; Claude only narrates. The drift service is a
FastAPI app on Fly.io (`tpm-drift-detector`, scale-to-zero) poked by a GitHub
Actions cron; the KPI stages run from launchd on a laptop.

## Layout

```
main.py           FastAPI: /healthz, POST /drift/run, GET /drift/findings
config.py         pydantic-settings; every secret optional so CI boots with none
observability.py  enable_llm_obs(): Datadog LLM Obs, no-op without DD_API_KEY
collectors/       Jira, program snapshots, billing (Anthropic admin, Heroku)
store/            SQLite snapshot store + Finding/Snapshot models
drift/            graph -> rules -> pipeline -> notify (Slack)
narrative/        the drift digest (Claude) + its markdown templates
kpi/              the KPI stages, Datadog generators, datadog_sync (pull/push/diff)
datadog/          exported JSON for the hand-built Datadog objects + manifest.json
evals/            billed eval subjects on agent-evals — dev extra, NOT in the image
simulate/         the scripted ten-week program in Jira, one day per tick
seed/             the drift-demo Jira scenario, idempotent by label
scripts/          kpi_daily.sh / kpi_weekly.sh + the launchd plists
grafana/          generated dashboards (kpi/dashboards.py)
docs/             one record per story (docs/kpi/, docs/delivery/, datadog-as-code.md)
tests/            pytest, offline; Jira fixtures in tests/fixtures/
```

The Dockerfile copies exactly the `[tool.setuptools] packages` list plus the
three entrypoint modules; a new package goes in both or the image build fails.

## Conventions (hold a change to these)

- **Rules decide, the model narrates.** No LLM judgment in `drift/rules.py` or
  in `kpi/` scoring; Claude writes the digest, the brief and the KPI draft. If a
  number is wrong, the bug is in Python.
- **Imports at the top.** No function-local imports. The one allowed exception
  is an optional dependency in a module-level `try/except ImportError` with a
  comment saying so (`observability.py`, ddtrace).
- **No auto-sync of infrastructure state.** Drift jobs detect and go red; a
  human commits the fix, in both directions. A scheduled job never writes to
  Jira, Datadog or Grafana; only a CLI asked with `--push`, `seed` or `tick` does.
- **Datadog objects are code.** Exported ones: edit `datadog/*.json`, run
  `python -m kpi.datadog_sync push`, then `diff` until clean. Generated ones:
  edit `kpi/datadog.py` and `--push`. Never hand-edit in the UI without pulling
  back, or the daily drift workflow goes red.
- **Config via `config.settings`** (pydantic-settings, `.env`, never
  committed); never `os.environ` for settings. Runtime secrets are Fly and
  Actions secrets; `DD_*`, `GRAFANA_TOKEN`, `EVAL_DATABASE_URL` live in
  `~/.zshrc` only.
- **ddtrace is a main dependency**: spans ship from the image. Every entry point
  that calls a model calls `enable_llm_obs(ml_app)`; `ml_app` names the agent
  (`kpi-agent`, `drift-digest`), not the repo.
- **Metric names are singular** (`github.code_scan_alert`); telemetry is
  org-scoped.
- **Source health is explicit.** A source that raised is `error`, one that
  answered nothing is `missing`; neither is a zero.
- **Scheduled jobs skip cleanly on a missing secret and fail loudly on failed
  work** (`set -o pipefail` when piping through `tee`). A skipped job looks
  like a passing one, so say so in the log.
- **Python 3.12**, `from __future__ import annotations`, type hints, dataclasses
  or pydantic for data. Ruff line-length 100, rules E F I UP B W. Docstrings say
  why and cite the ticket (`RC1-NNN`).

## Testing

- `uv run --extra dev pytest` from the repo root (`testpaths = tests`). Tests run
  offline: Jira through the JSON fixtures in `tests/fixtures/`, HTTP through
  `httpx.MockTransport`, the model through a fake client. No API keys.
- `kpi.RUBRIC_VERSION` and `docs/kpi/rubric.md` must agree; a test asserts it.
- `evals/checks.py` is tested against bad output, not just good.
- `python -m evals --list` is free; running a subject is BILLED and recorded.

## Commands

```bash
uv run --extra dev pytest                        # the suite, offline
uv run --extra dev ruff check .                  # lint
uv run uvicorn main:app --reload                 # the service locally
uv run python -m drift.pipeline                  # one drift cycle from the CLI
uv run python -m kpi.datadog_sync pull|diff|push # exported objects <-> account
uv run python -m kpi.datadog dashboards --push   # generated objects (or monitors)
uv run python -m simulate seed|tick|verify       # the simulated program
```

Run everything through `uv run`; `.python-version` pins 3.12.

## Workflow

- One branch per ticket, `rc1-NNN-slug`; never commit on `main`. Commit subject
  `RC1-NNN: what changed`, a short body, no Co-Authored-By trailer. Claude opens
  the PR; Reid merges.
- Every story leaves a record in `docs/` with its numbers.
- The five Actions workflows: `ci.yml` (ruff + pytest on every PR and push to
  main); `fly-deploy.yml` (push to main → `flyctl deploy` → gate on three
  consecutive healthy `/healthz` reads, because a stopped machine reports
  "warning", not "critical"); `drift-daily.yml` (12:17 UTC, POSTs `/drift/run`);
  `datadog-drift.yml` (13:23 UTC and any PR touching `datadog/**`, runs
  `datadog_sync diff`); `security-posture.yml` (11:41 UTC, posts scanner alert
  counts). Every production path is Actions-driven; Heroku auto-deploy stays off.
