# Code security — the decision (RC1-338)

**Decision: do not buy Datadog Code Security. Turn on GitHub's native
scanners instead — they are free on public repositories, and all five repos
in this estate are public — and let the Datadog GitHub integration carry
their alert state onto the delivery dashboard, which costs nothing either.**
The pr-review-agent keeps the pull-request conversation to itself; scanners
report to the Security tab and to Datadog, not into the PR thread.
Enabling is a separate story, RC1-359, filed from this spike; the SKU question
reopens if a repo goes private or a second committer shows up.

Time-boxed to one session, as the ticket asked. The price list and the
account's own bill settled the cost question in the first hour; the rest
of the session went on measuring what a scanner would actually find today,
so that the "no" is a "no, and here is what we are not missing" rather than
a shrug.

## The two questions the ticket asked

### 1. Cost — what is the line item?

Datadog's public price list (US site, read 2026-09-01), per committer per
month:

| SKU | annual | on-demand |
| --- | --- | --- |
| Static SCA (vulnerable dependencies) | $25 | $36 |
| SAST (first-party code) | $25 | $36 |
| Secret Scanning | $15 | $22 |
| Code Security Bundle (all of the above + IaC) | $40 | $57.60 |
| *for scale:* CI Pipeline Visibility (already on) | $8 | $12 |

A committer is a Git author email with three or more commits in a month in
a repository where the product is enabled; verified bots are not billed.
This estate has exactly one — the CI Visibility usage line reads
`ci_visibility_pipeline_committers_hwm: 1` for August — so the bundle is
$57.60 a month on the plan the account is actually on (everything here is
on-demand; nothing is committed annually) and $40 if it were.

Against what the account spends: the estimated-cost API puts **August 2026
at $0.18**, all of it on-demand Synthetics API tests. The CI Visibility
committer shows in usage but has not produced a charge. So the bundle would
be roughly a 300x increase in the monthly bill, and SCA alone a 200x
increase, for a capability GitHub already includes on public repositories
at no charge:

| capability | Datadog SKU | GitHub, public repo |
| --- | --- | --- |
| vulnerable-dependency alerts | Static SCA | Dependabot alerts + security updates — free |
| first-party static analysis | SAST | CodeQL code scanning (default setup) — free |
| committed secrets | Secret Scanning | secret scanning + push protection + validity checks — free |
| PR comments / gates | all three | Dependabot PRs, code-scanning PR checks, push protection at push time — free |

None of the GitHub features is on today, which is the actual finding of
the cost question — see the enablement gaps below. The Datadog GitHub
integration's telemetry ("Code Scan Alerts", "Secret Scan Alerts", plus
Dependabot alert events over the webhook) collects GitHub's alert state
into Datadog metrics with read-only App permissions and no billable product
enabled, so the Datadog surface — the reason this epic exists — gets a
security posture pane without the SKU.

**When the answer changes.** GitHub's scanners are free *because the repos
are public*. On a private repo GitHub bills its own per-committer SKUs at
roughly the same order of magnitude as Datadog's, and the comparison is
then feature-for-feature rather than free-versus-paid. A second committer
doubles both sides equally. Neither is in view.

### 2. Coexistence — do Datadog's PR comments complement or duplicate the pr-review-agent?

**Complement in what they find; duplicate in where they say it.** The two
answer different questions:

- **The agent's `dependencies` category is judgment.** On pr_agent#25 it
  flagged `ddtrace>=4.14` as unbounded in a runtime requirements file
  ("every `fly deploy` ... will pull the latest ddtrace release"), which is
  the finding that put `<5` caps across the estate; on the same PR it
  noted the `agent-evals` git pin was a mutable tag, not a SHA. Neither is
  a CVE. No vulnerability database would have produced either finding.
- **SCA is lookup.** It matches exact pinned versions against advisories,
  and — the part the agent structurally cannot do — it keeps matching after
  the PR merges, when a new advisory lands against an old pin. The agent
  runs once per PR, on the diff; across 13 recent platform PRs (#38–#51)
  it posted 76 inline comments and none were `dependencies` findings,
  because those PRs did not touch dependencies. SCA's value is exactly on
  the days nobody opens a PR.
- **SAST overlaps the agent's `security` category** (injection, unsafe
  deserialization, shelling out) but with rules instead of reading. The
  agent explores the repo for context; SAST pattern-matches. Below, SAST's
  one real signal on this code is string-built SQL — a class the agent would
  also read, with the advantage of knowing whether the interpolated value is
  a table name or user input.

So both are wanted. What is **not** wanted is three voices in one PR thread:
the agent's review, a scanner's inline comments, and Dependabot's own PRs.
The agent already carries the one-voice rule in its prompt ("over-flagging
trains people to ignore reviews"), and its `leaked_secret` category is the
only gate. The rule this spike sets: **scanners post to the Security tab
and to Datadog, not into the PR conversation; push protection blocks at
push time, before there is a PR; the agent stays the single reviewer
voice.** If a scanner finding should reach the PR, the right shape is the
one the n8n cost check already uses — feed it to the agent as context so
it lands in the one review — and that is RC1-106 work, not this epic's.

## What a scanner would find today (measured)

Run locally on 2026-09-01 against the checked-out repos and, for the
portfolio site, against the manifests read from GitHub. Free tools standing
in for the SKUs: the OSV database for SCA, `bandit` for SAST, `npm audit`
for the two JavaScript apps, `detect-secrets` for the working trees.

**SCA — Python (OSV, exact pins).** Two of the five sets have lockfiles
(`uv.lock` in launch-planner-agent and agent-evals); the platform and
pr_agent install from unpinned ranges at image build, and the portfolio
site from an unpinned `requirements.txt`, so those three were resolved with
`uv pip compile` to see what a build today would ship.

| repo | pinned packages | vulnerable |
| --- | --- | --- |
| tpm-automation-platform | 43 | 0 |
| pr_agent | 39 | 0 |
| launch-planner-agent | 65 | 0 |
| agent-evals | 16 | 0 |
| reid_basic | 111 | **1** — `chromadb==1.5.9` (GHSA-2wm9-hf6c-p5cr, GHSA-36p7-vc44-83pf, GHSA-f4j7-r4q5-qw2c, GHSA-xph7-9rjv-w5fr, PYSEC-2026-311) |

**SCA — JavaScript (`npm audit`, lockfiles).** Every advisory has a fix
available; all are build/test toolchain, none ship to a browser.

| lockfile | critical | high | moderate |
| --- | --- | --- | --- |
| launch-planner-agent `apps/web` | 1 (vitest) | 2 (vite, nanoid) | 4 |
| reid_basic | 0 | 5 (brace-expansion, browserslist, js-yaml, nanoid, undici) | 1 |

**SAST (`bandit`, tests excluded).** One signal: 22 `B608` string-built SQL
statements — 19 in the platform's `kpi/readings_store.py`,
`kpi/escalations_store.py` and `kpi/dashboards.py`, 3 in launch-planner's
`apps/api/app/store.py`. These read as table/column-name interpolation, not
request input, but that is exactly the triage a SAST tool hands back to a
human and a reviewer does in its head; worth a pass when the enabling story
turns CodeQL on. Everything else is LOW — launch-planner's 1,402 `B101`
asserts are the classic noise an unconfigured SAST run produces, and the
first thing a ruleset would suppress.

**Secrets.** GitHub secret scanning is already on, with push protection,
for tpm-automation-platform, launch-planner-agent and agent-evals — **0
alerts** on all three. It is **off** on pr_agent and reid_basic, which is
a settings toggle away. `detect-secrets` over the four local working trees
found 8 candidates, all in tests and a runbook (fixture keys, a test
private key, a basic-auth string in a SQL-store test) — the false-positive
class every secret scanner produces and that a baseline file silences.
Validity checks (GitHub's own "is this key live?" step, the counterpart to
Datadog's "third-party active validation") are off everywhere.

**Reading the table.** The estate is, today, clean where it is measured and
unmeasured where it matters most for the future: the deployed images of
the platform and pr_agent resolve their dependencies at build time from
ranges, so the exact set running in production is knowable only from the
build log. RC1-337 baked the *code* SHA into the images; nothing pins the
*dependency* set. That is a lockfile decision, and it belongs in the
enabling story, because both Dependabot and any SCA product do their best
work against a lockfile.

## Enabled 2026-09-02 (RC1-359)

The gaps in the next section were closed on all five repos by `gh api`, in
this order: the pr-review-agent first learned to acknowledge and never
review PRs authored by `dependabot[bot]` (pr_agent PR #36, Fly release
v20; `REVIEW_SKIP_AUTHORS`), so a burst of version-bump PRs can never
become a burst of billed reviews; then Dependabot alerts, CodeQL default
setup, secret scanning and push protection were turned on. Dependabot
security updates stay off: alerts say what to fix, and RC1-360 showed the
fixes are ordinary by-hand work. No scanner opened a PR or commented on
one. Datadog Code Security was not touched.

First analysis, same day (open alerts, Security tab only):

| repo | CodeQL | Dependabot |
| --- | --- | --- |
| tpm-automation-platform | 3 (`actions/missing-workflow-permissions` in all three workflows) | 0 |
| pr_agent | 2 (same rule) | 0 |
| launch-planner-agent | 6 (`py/path-injection` ×3 in `apps/api/app/main.py`, `js/xss-through-dom`, workflow perms ×2) | 6 (npm in `apps/web`: vitest critical, vite high + 2 medium, postcss, esbuild) |
| agent-evals | 1 (workflow perms) | 0 |
| reid_basic | 9 (clear-text logging ×2, path-injection, redos, insecure-randomness, `actions/untrusted-checkout/high` in `heroku-release.yml`, workflow perms ×2) | 4 (`chromadb` in `requirements.txt`: 2 critical, 2 high) |

Still open, both account-owner clicks: secret-scanning **validity checks**
(the repo PATCH returns 200 and leaves the setting off on user-owned
repos; GitHub settings pages do not load through the browser tooling), and
the Datadog GitHub App's read on the three alert types. The dashboard row
follows the second.

## Enablement gaps found by the spike (all free, none on at the time)

| repo | Dependabot alerts | code scanning (CodeQL) | secret scanning + push protection |
| --- | --- | --- | --- |
| tpm-automation-platform | off | not configured (python, actions) | **on** |
| pr_agent | off | not configured (python, actions) | off |
| launch-planner-agent | off | not configured (python, js/ts, actions) | **on** |
| agent-evals | off | not configured (python, actions) | **on** |
| reid_basic | off | not configured (python, js/ts, actions) | off |

No repo has a `dependabot.yml`; no repo has ever run a code-scanning
analysis. CodeQL's default setup already detects the right languages on
each.

## What this spike did not do

- **Did not enable Datadog Code Security to trial it.** Enabling is what
  starts the committer meter; the price list and the free alternative made
  a trial's answer irrelevant to the decision. Nothing in the account's
  usage or estimated-cost data shows any Code Security product as ever
  having been on.
- **Did not verify the Datadog GitHub App's current permission set.** The
  App was installed 2026-08-30 for events and source-code links; whether it
  also holds read on code-scanning, secret-scanning and Dependabot alerts
  (what the telemetry needs) is a GitHub settings page
  (`github.com/settings/installations`) that would not load through the
  browser tooling this session. It is the first step of the enabling story.
- **Did not fix the 14 advisories it found.** chromadb and the two npm
  sets are ordinary bump-and-test work in reid_basic and launch-planner;
  filed as RC1-360, not folded into a spike.

## Follow-ups

1. **RC1-359 — enabling story (under RC1-333):** Dependabot alerts + grouped security
   updates on all five repos; CodeQL default setup on all five; secret
   scanning, push protection and validity checks on pr_agent and
   reid_basic; grant the Datadog GitHub App read on the three alert types
   and add the alert metrics to the delivery dashboard `izc-5s7-tz8`;
   decide lockfiles for the platform and pr_agent images. Scanner output
   goes to the Security tab and Datadog, never as PR comments.
2. **RC1-360 — portfolio site dependencies (under RC1-215):** bump `chromadb` off 1.5.9 and take the
   six `npm audit` fixes in reid_basic; the seven in launch-planner's
   `apps/web` ride along with whoever next touches that app.
3. **Optional, RC1-106:** feed open Dependabot/code-scanning alerts to the
   pr-review-agent as context, the way the n8n cost check already is, so
   scanner facts arrive in the one review voice.
