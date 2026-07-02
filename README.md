# TPM Automation Platform

Proactive program-management tooling that watches a Jira project and surfaces
**what's about to go wrong** — not just where things stand today.

The first service is the **Dependency Drift Detector**: it scans Jira
dependency links (`blocks` / `is blocked by`) and flags cases where an upstream
ticket slipped but the downstream schedule hasn't reacted — *before* the
collision lands. Deterministic Python decides what's drifting; Claude only
writes the narrative.

## The story: prototype → limits → platform

- **v1 — n8n prototype.** A stakeholder status email built in n8n proved the
  idea: pull from Jira, let Claude write the update, deliver it. Great for
  "here's where we are," but visual-workflow logic hit its ceiling — no real
  types, no unit tests, awkward multi-step state, hard to evolve.
- **The limit.** Drift detection needs a dependency graph, changelog diffing,
  per-run snapshots, and scored rules. That's application logic, not
  drag-and-drop nodes.
- **v2 — this platform.** A typed, tested Python codebase deployed on Fly.io,
  with the logic in a real repo (CI, Pydantic models, pytest). Scheduling is a
  plain GitHub Actions cron that pokes the service — n8n was dropped once the
  logic left it; keeping a whole SaaS around just to fire a daily HTTP request
  wasn't worth the moving part.

> _Evolved from the v1 n8n status-email project. Links: TODO — add the v1 repo
> URL(s) here once public._

## What it detects

| Rule | Fires when |
|------|-----------|
| **Timeline inversion** | Upstream due date is after the downstream start/due date |
| **Unabsorbed slip** | Upstream due date moved later (per changelog) but downstream dates haven't budged since |
| **Lead-time risk** | Upstream still not started inside the downstream's lead-time window |
| **Transitive risk** | A transitive blocker has itself entered **Blocked** |

Each finding is scored `days_of_overlap × downstream_priority × proximity` and
bucketed 🔴 collision imminent / 🟡 at risk / ⚪ watch.

## Module map

```
main.py         FastAPI entrypoint — GET /healthz, POST /drift/run
config.py       env-backed settings (pydantic-settings)

collectors/     data acquisition (Jira issues, links, changelog)    [3/9]
store/          append-only SQLite snapshots + findings table        [4/9]
drift/
  graph.py      networkx dependency DAG                              [5/9]
  rules.py      deterministic detection + severity scoring           [6/9]
  notify.py     Slack owner DMs + program-channel rollup             [8/9]
  pipeline.py   collect -> ... -> notify, one run + JSON log         [9/9]
narrative/      findings -> TPM-voiced digest via Anthropic SDK      [7/9]
seed/           idempotent RC1 demo-data seeder                      [2/9]
tests/          pytest, fixture-driven (no live API calls)
```

`collectors/`, `store/`, and `narrative/` are shared with the planned
status-email v2 service.

## Pipeline

```
Scheduler (GitHub Actions daily cron -> POST /drift/run)
  -> collector   httpx  -> Jira search + changelog API
  -> graph       networkx dependency DAG
  -> store       SQLite snapshot per run  (drift = diff vs. last run)
  -> rules       deterministic findings + severity
  -> narrative   Anthropic SDK -> TPM-voiced digest
  -> notify      Slack DM to owners + weekly rollup
```

## Getting started

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env   # fill in Jira / Anthropic / Slack credentials

uvicorn main:app --reload      # then GET http://127.0.0.1:8000/healthz
pytest                         # run the test suite
ruff check .                   # lint
```

Targets **Python 3.12**. Secrets are all optional at boot — the health check
and import graph work with no credentials, so CI runs green without them.

## Run a drift cycle

```bash
python -m drift.pipeline          # one run: collect -> ... -> notify + JSON summary
# or hit the API:
uvicorn main:app --reload
curl -X POST localhost:8000/drift/run -H "X-Drift-Token: $DRIFT_RUN_TOKEN"
```

Set `DRY_RUN=true` to log notifications instead of posting to Slack. Each run
emits one structured JSON line (counts per rule, buckets, notify outcomes,
duration) for logs/observability.

## Deploy (Fly.io)

Containerized (`Dockerfile`, slim Python 3.12) with SQLite on a mounted volume:

```bash
fly launch --no-deploy
fly volume create drift_data --size 1
fly secrets set JIRA_BASE_URL=... JIRA_EMAIL=... JIRA_API_TOKEN=... \
                ANTHROPIC_API_KEY=... SLACK_WEBHOOK_URL=... DRIFT_RUN_TOKEN=...
fly deploy
```

**Scheduling:** `.github/workflows/drift-daily.yml` is a GitHub Actions cron
that POSTs `/drift/run` daily — set repo secrets `DRIFT_URL` and
`DRIFT_RUN_TOKEN` to enable it. The Fly machine auto-stops when idle and the
cron wakes it.

## Demo

A live run against the seeded RC1 scenario posts this to Slack (📸
`docs/slack-digest.png`):

```
*RC1: checkout API timeline inversion threatens web client integration*
This run has 2 red, 1 yellow, and 2 white findings, all new; none resolved.
🔴 RC1-158 (Integrate checkout API) due 2026-07-16 but upstream RC1-157 due
   2026-07-20 — a 12d overlap; owner Reid Collins.
🔴 RC1-160 (Launch readiness) unchanged despite RC1-159 slipping 14d.
🟡 RC1-162 starts 2026-07-04 with ~1 working day lead; upstream not started.
⚪ RC1-164 / RC1-165 downstream of Blocked RC1-163.
```

## Status

Complete — epic **RC1-131**, stories `[1/9]`–`[9/9]`. Deterministic detection
(4 rules, fixture-tested), Claude narrative, Slack delivery, and a daily
scheduled run against RC1. Evolved from the v1 n8n status-email prototype.
