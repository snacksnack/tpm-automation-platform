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
- **v2 — this platform.** A typed, tested Python codebase. Scheduling still
  lives in n8n (the hybrid architecture is intentional — see
  `V2-ARCHITECTURE-NOTES.md`), but the logic moves into a real repo with CI,
  Pydantic models, and pytest.

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
narrative/      findings -> TPM-voiced digest via Anthropic SDK      [7/9]
tests/          pytest, fixture-driven (no live API calls)
```

`collectors/`, `store/`, and `narrative/` are shared with the planned
status-email v2 service.

## Pipeline

```
Scheduler (n8n cron / GitHub Actions daily)
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

## Status

Bootstrapped under epic **RC1-131**. Child stories `[1/9]`–`[9/9]` are executed
in order; `[2/9]` (seed demo data) runs in parallel with `[1/9]`.
