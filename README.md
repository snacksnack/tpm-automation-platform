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

> _Evolved from the v1 n8n status-email prototype:
> [n8n-stakeholder-status-email](https://github.com/snacksnack/n8n-stakeholder-status-email)._

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
main.py         FastAPI entrypoint — GET /healthz, POST /drift/run,
                GET /drift/findings (read-only, no side effects)
config.py       env-backed settings (pydantic-settings)

collectors/     data acquisition (Jira issues, links, changelog);
                program snapshots, one per run, per-source health  [3/9, RC1-301]
store/          append-only SQLite snapshots + findings table        [4/9]
drift/
  graph.py      networkx dependency DAG                              [5/9]
  rules.py      deterministic detection + severity scoring           [6/9]
  notify.py     Slack owner DMs + program-channel rollup             [8/9]
  pipeline.py   collect -> ... -> notify, one run + JSON log         [9/9]
narrative/      findings -> TPM-voiced digest via Anthropic SDK      [7/9]
evals/          drift-digest goldens — frozen cases, scored        [RC1-261]
                + kpi-ledger: KPI readings vs the ground truth        [RC1-300]
kpi/            Program KPI agent — define stage: brief + rubric ->
                reviewable KPI tree (docs/kpi/); the reading contract [RC1-302]
seed/           idempotent RC1 demo-data seeder                      [2/9]
simulate/       scripted 10-week program in PMA, one sim-day per
                tick; the KPI agent's test fixture, and its
                ground-truth ledger                                   [RC1-299, RC1-300]
tests/          pytest, fixture-driven (no live API calls)
```

`collectors/`, `store/`, and `narrative/` are shared with the planned
status-email v2 service.

## How we know the digest is any good

The digest is the one part of this system a model writes, so it is the one part
that needs measuring rather than asserting. `evals/` scores it against frozen
finding sets, using the shared harness
[`agent-evals`](https://github.com/snacksnack/agent-evals) (pinned by tag) —
its README is the methodology. Billed runs publish to the shared
**[quality trend page](https://snacksnack.github.io/agent-evals/)**; taking a
measurement end to end (keys, store, publish step) is the library's
[runbook](https://github.com/snacksnack/agent-evals/blob/main/docs/measuring.md).

```bash
python -m evals run drift-digest-allclear   # free, deterministic — runs in CI
python -m evals run drift-digest            # billed, needs ANTHROPIC_API_KEY
python -m evals degrade                     # billed — are the checks awake?
python -m evals template-drift              # declared prompt version vs its hash
python -m evals run kpi-ledger              # free — the KPI agent's subject (RC1-300, below)
```

**Nothing here uses an LLM judge.** The prompt template states its rules as
absolutes — never invent, never re-rank, one line per finding, this order, that
glyph — and almost all of them are exactly checkable. On this subject a regex is
*more* accurate than a judge and free, so all six gating characteristics are
deterministic. The only advisory one is whether the summary spells its bucket
counts as digits, because "two red" is a correct answer a checker cannot read.

The split between the two subjects is by cost, not by importance. The
empty-findings all-clear path never calls the API, so it is scored on every push
— and it is checked with a client that raises if touched, because "did not call
the model" is the actual promise, and asserting the cost was zero would also
pass if the call were made and discarded.

### The checks are tested against bad output, not just good

A suite that passes on its first run has said either "the subject is good" or
"the checks are asleep", and cannot tell you which. Two things settle it:

* `tests/test_evals_checks.py` hands each check a digest that is wrong in
  exactly one way and asserts it says so. Free, and in CI.
* `python -m evals degrade` removes one rule from the prompt template and reruns
  the same cases, to see whether the matching characteristic goes red.

The degrade run says **2 of 5 rules are load-bearing**: without the glyph rule
the model stops emitting 🔴/🟡/⚪, and without the ordering rule it leaves a
stale red ahead of a new one. The other three — never re-rank, one line each,
never invent — hold up with the rule removed, because the output schema already
forces them: `bucket` and `downstream` are fields copied from the payload.

That is a finding about the *prompt*, not the checks, and it only became
readable once the checks were independently known to fail on bad output.

## Program KPI agent (RC1-298)

The second service, landing one stage at a time. Where the drift detector
answers "what is about to slip", this answers "which numbers should the SVP
be looking at, and are they real". The judgment is made under a versioned
rubric — [`docs/kpi/rubric.md`](docs/kpi/rubric.md) — and every stage records
which version it applied.

The define stage is in: a program brief plus the rubric go to the model, a
KPI tree comes back as structured output, and the code refuses any draft
that breaks the rubric's shape (1–2 outcomes, 3–4 leading indicators, every
leading indicator naming its outcome, every Goodhart risk above low paired
with a counter-metric). The model never sees the hand-written baseline; the
two are compared afterwards in a review document.

```bash
python -m kpi.define --program docs/kpi/programs/simulated-program.md \
    --out docs/kpi/trees/simulated-program.agent.md     # billed, one call
```

```
docs/kpi/rubric.md                  the rubric, versioned
docs/kpi/programs/<program>.md      what the agent is given
docs/kpi/trees/<program>.md         hand-written baseline, written first
docs/kpi/trees/<program>.agent.md   the agent's draft (+ .json twin)
docs/kpi/trees/<program>.review.md  where they disagree, and who was right
docs/kpi/simulator.md               runbook: the clock, the daily tick, jumping, teardown
docs/kpi/snapshots.md               the collector: one dated snapshot per run per program
docs/kpi/ledger.md                  the ground truth: how each expected reading is derived
docs/kpi/ledger/<program>.csv       the ledger itself, regenerated from the scenario
```

### The simulated program (RC1-299)

The KPI agent is verified against a program whose every number is known in
advance: thirty-four stories in four workstreams under a dedicated PMA epic,
advancing one simulated day per tick, with four planted events — a scope add
in week 3, an upstream slip in week 5, a cost spike in week 6, and a silent
source break in week 7 (the label the collector keys on is dropped for five
days, and nobody comments). `simulate/scenario.py` is the program as data;
`state_at(day)` is the exact Jira state for any day, and the ground-truth
ledger (RC1-300) derives from the same function the simulator converges to.

```bash
python -m simulate seed              # create / converge to day 0
python -m simulate tick [--days N]   # advance the clock and converge
python -m simulate to-day 45         # jump (development)
python -m simulate verify            # Jira == scenario for the current day?
python -m simulate status
python -m simulate teardown          # delete every simulated issue, forget the clock
```

Seed, tick and verify are one computation — a diff between `state_at(day)`
and what Jira shows — so a converged day always verifies, and converging day
by day is the same as jumping. The clock, a manifest of keys, and the weekly
cloud-spend line live in `data/kpi-sim/` (gitignored); the collector reads the
sim-date from there. `scripts/launchd/` has the daily-tick plist (07:00
local, one sim-day per calendar day); it is not installed automatically —
[`docs/kpi/simulator.md`](docs/kpi/simulator.md) is the runbook for the
clock, the tick, jumping ahead, and teardown.

### The ground-truth ledger and its suite (RC1-300)

What every KPI should read on every sim-day, derived from the same
`state_at(day)` the simulator converges to — 420 rows, six KPIs × seventy
days, each a `kpi.reading.Reading` (value, `ok`/`stale`/`broken`, tripped,
as-of date, reason) plus a tolerance. No value is ever zero for "unknown":
before the first spend row the cost KPIs read *stale* with a reason, and
through the week-7 source break the Jira KPIs read *broken* carrying day 42's
value with day 42's date. [`docs/kpi/ledger.md`](docs/kpi/ledger.md) has the
derivation, the decisions the tree left open, and what each planted event
looks like in the numbers.

```bash
python -m simulate ledger [--day N]                        # print it, or one day with the working
python -m evals run kpi-ledger                             # 70 cases, free, gates CI; recorded
python -m evals run kpi-ledger --impl no-break-detection   # a deliberately wrong one: fails days 43-47
```

`kpi-ledger` is the KPI agent's eval subject: one case per day, one
characteristic per KPI, and `detects-<event>` on the day after each planted
event — the first-day detector must read tripped (or broken). The
implementation is pluggable; until the track stage (RC1-305) lands the
reference is the ledger's own derivation, and two deliberately wrong ones
show the suite can fail. Wrong-implementation runs are never recorded.

### Snapshots (RC1-301)

KPIs are computed from dated snapshots, not Jira's changelog — transitions
cannot be backdated, so simulated time would not survive. `python -m
collectors snapshot <program>` reads every source a program names (its Jira
issues by label, the spend line, the eval store's run rows, the sim clock)
into one `ProgramSnapshot` stamped with both wall-clock and sim-date, with a
health row per source: `ok`, `missing` (answered with nothing — the week-7
break arrives this way) or `error` (could not be read — the section is
absent, never empty). Snapshots land in the drift detector's own store, the
same `runs` table widened, so the Portfolio Console (RC1-233) and the KPI
agent read one history. A day's KPIs recompute from its stored snapshot
alone — `simulate.ledger.derive(series=…)` over collected snapshots equals
the scenario-derived ledger, and a test proves it end to end offline.
[`docs/kpi/snapshots.md`](docs/kpi/snapshots.md).

```bash
python -m collectors snapshot simulated-program    # collect, store, print health; exit 1 if a source is not ok
python -m collectors runs simulated-program        # every stored run with its health
python -m collectors show simulated-program --sim-date 2026-10-20
```

Three PMA prerequisites, all configured on 2026-08-22: story points go through
the Agile estimation endpoint for board 68 (the board-correct route, and it
works whether or not the field is on a screen — it now is); the workflow has
a global **Blocked** status, which the KPI tree's blocked-share reads
directly; and teardown needs *Delete Issues*, which the default software
scheme grants only to the project's Administrators role.

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

## Read findings without running a cycle

`POST /drift/run` is not a read — it collects from Jira, writes rows, calls
Anthropic, and posts to Slack. Anything that only wants to *look* at drift (a
dashboard, a digest, the [launch-planner MCP
server](https://github.com/snacksnack/launch-planner-agent)) uses these instead.
They only ever SELECT, so they can be polled freely without sending a single
Slack message.

```bash
curl localhost:8000/drift/findings                      # the last stored run
curl "localhost:8000/drift/findings?bucket=red"         # red | yellow | white
curl "localhost:8000/drift/findings?rule=unabsorbed_slip"
curl "localhost:8000/drift/findings?since_run=12"       # first seen at/after run 12

# one finding, with its evidence
curl "localhost:8000/drift/findings/timeline_inversion/RC1-158?upstream=RC1-157"
```

Every response carries `run_id` and `run_at`: these report the **last scheduled
run**, not a fresh scan. No run yet is a valid state — an empty list with a null
`run_id`, which is different from "no drift".

A finding is addressed by its identity, `(rule_type, upstream, downstream)` —
the same triple the store uses to carry `first_seen_run` across runs — rather
than by a row id. Findings are re-derived on every run, so a row id would point
at a different finding as soon as the scheduler fired again, and a caller that
listed findings and then asked about one would silently get the wrong answer.

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
