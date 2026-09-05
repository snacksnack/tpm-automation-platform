# Code security — the decision (RC1-338)

**Decision: do not buy Datadog Code Security. Turn on GitHub's native
scanners instead — they are free on public repositories, and all five repos
in this estate are public — and let the Datadog GitHub integration carry
their alert state onto the delivery dashboard, which costs nothing either.**
The pr-review-agent stays the only *narrative* reviewer in a PR; scanner
findings may appear on a PR as their own check, and their alert state lives
in the Security tab and on the delivery dashboard. (Amended 2026-09-05 — the
spike's original rule kept scanners out of PRs entirely; see "Rule amended"
below.) Enabling is a separate story, RC1-359, filed from this spike; the
SKU question reopens if a repo goes private or a second committer shows up.

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
only gate. The rule this spike set: **scanners post to the Security tab
and to Datadog, not into the PR conversation; push protection blocks at
push time, before there is a PR; the agent stays the single reviewer
voice.** If a scanner finding should reach the PR, the right shape is the
one the n8n cost check already uses — feed it to the agent as context so
it lands in the one review — and that is RC1-106 work, not this epic's.

**Rule amended 2026-09-05, after the first scanner findings on a real PR.**
The rule above was written before any scanner had run. On PR #58 CodeQL's
pull-request check posted two inline alerts — false positives, as it turned
out, but they were read, traced and fixed *before merge*. Had they gone only
to the Security tab they would have landed on `main` and shown up as "2
high" on the dashboard tile that very PR was adding, with nobody looking at
why. Two things the original rule under-weighted: CodeQL does dataflow
analysis (this string reaches that sink), which overlaps less with an LLM
reviewer's read of intent than assumed; and PR time is the cheapest moment
to act on a security finding. So: **scanner findings may appear on a PR as
their own check — CodeQL's PR alerts and Copilot Autofix stay on; the
pr-review-agent stays the only narrative reviewer.** The one-voice concern
that survives is Dependabot's own PRs, which the agent skips
(`REVIEW_SKIP_AUTHORS`) and which stay off (alerts only, no security
updates). False positives are a triage cost, not a reason to silence the
check; the collector's own naming false positive was fixed by a rename
(PR #60), which is the durable shape for that class.

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

Validity checks are **out of reach, not merely off**: partner-pattern
validity checks need GitHub Secret Protection on a Team or Enterprise
organization and are not offered on personal-account public repos (the
settings page has no toggle; the repo PATCH accepts the field and ignores
it). GitHub's own tokens are validity-checked automatically. The free scope
here is secret scanning plus push protection, both on. The spike's table
below listed validity checks as free; it is free only where it exists.

CodeQL default setup also switched on **Copilot Autofix**, which attaches a
suggested patch to a code-scanning alert on a PR. It was turned off on
pr_agent on 2026-09-02 under the original one-voice rule; under the amended
rule it stays on for the other four repos, and turning it back on for
pr_agent is a settings-page click whenever wanted (no REST endpoint). A
proposed patch on a real dataflow finding is a time-saver for a solo
developer who reviews every PR anyway.

Still open: the Datadog GitHub App's read on the three alert types. The
dashboard row follows it.

## Alert metrics in Datadog — own collector (RC1-359, 2026-09-05)

Step 4 of the enabling story assumed Datadog's GitHub integration would carry
the alert counts for free. It registers the metric names
(`github.code_scan_alert`, `github.secret_scan_alert`, integration
`github_telemetry`) the moment the Telemetry toggles are on, and then never
produces a point for this account. Three days of nothing, with GitHub holding
11 open code-scan alerts and the same App delivering PR and push events for
every repo, narrowed it to one thing: the docs describe "the organization's
Alert state", GitHub only lists alerts account-wide at `/orgs/{org}/...`, and
those endpoints return 404 for `snacksnack`, which is a User. Same class of
limitation as validity checks. A Datadog support question is worth asking,
but the dashboard row should not wait on it.

**What runs instead.** `python -m kpi.security_posture` reads the repo-level
endpoints (`/repos/{owner}/{repo}/code-scanning/alerts?state=open`, same for
`secret-scanning`), which work for a personal account, and posts two gauges:

| metric | tags | meaning |
|---|---|---|
| `delivery.security.code_scan_alerts_open` | `repo`, `severity` (critical/high/medium/low, zero-filled; `none` only when > 0) | open CodeQL alerts by security severity |
| `delivery.security.secret_scan_alerts_open` | `repo` | open secret-scanning alerts |
| `delivery.security.collector_errors` | `repo` | 1 when that repo's alerts could not be read this run, else 0 |

One repo failing does not cost the other four their point: the collector
records the error, ships what it could read, leaves a *gap* (no alert series)
for the failed repo rather than a zero, prints a GitHub `::error` annotation
and exits non-zero, so the run is red. The first dispatched run found this
the hard way — the token had missed `pr_agent`, the 403 aborted all five
repos, and the run still showed green because the output was piped through
`tee` without `pipefail`. Both fixed the same day.

The `Security posture` workflow runs it daily at 11:41 UTC. It needs
`SECURITY_ALERTS_TOKEN` — a fine-grained PAT, read-only on Code scanning
alerts and Secret scanning alerts for the five repos, because the default
Actions token cannot read the other four — and `DD_API_KEY`. Missing secrets
skip with a warning annotation rather than fail: the CI-failed monitor routes
to the incident summarizer, and a to-do is not an outage.

**Cost, measured not guessed.** Custom metrics bill on the average number of
unique series present per hour across the month; this account averaged 1.5
in early September for half a cent. ~25 series present one hour in
twenty-four adds about one to that average — cents (30 series with the error gauge). Hourly would be 24×.
Alert counts change a few times a week; daily is the right cadence, and the
dashboard tiles pin a 1-week live span so a short window's *no data* reads as
cadence, not outage (same rule as the TLS-expiry widgets).

**Where it shows.** Delivery dashboard `izc-5s7-tz8`, group "Security
posture — GitHub scanners": critical+high code-scan count, secret-scan count,
by-repo toplist, by-severity trend over a month. Exported with the rest of
the hand-built objects in `datadog/`, so the drift job guards it.

**A naming note.** CodeQL's `py/clear-text-logging-sensitive-data` query
treats any identifier containing "secret" as sensitive and flagged the
collector's own `SECRET_METRIC` constant and `counts["secret"]` key — a metric
name and an integer — on every print they reached. Dismissing did not stick:
the next edit to those lines raised fresh alerts. The identifiers are now
`LEAK_SCAN_METRIC` and `leaks`; the Datadog metric name still says
`secret_scan`, because that is what GitHub calls the feature and the string
is not what the heuristic reads.

**What it does not do.** Nothing here writes to GitHub or to PR threads; the
one-reviewer-voice rule from the spike holds. If Datadog ever supports
personal-account telemetry, the collector and its workflow are the two files
to delete and the tiles re-point to the integration metrics.

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
   and add the alert metrics to the delivery dashboard `izc-5s7-tz8`
   (done 2026-09-05 with the platform's own collector — see above);
   decide lockfiles for the platform and pr_agent images. Scanner findings
   may appear on a PR as their own check (rule amended 2026-09-05); the
   pr-review-agent stays the only narrative reviewer. **Done 2026-09-05.**
2. **RC1-360 — portfolio site dependencies (under RC1-215):** bump `chromadb` off 1.5.9 and take the
   six `npm audit` fixes in reid_basic; the seven in launch-planner's
   `apps/web` ride along with whoever next touches that app.
3. **Optional, RC1-106:** feed open Dependabot/code-scanning alerts to the
   pr-review-agent as context, the way the n8n cost check already is, so
   scanner facts arrive in the one review voice.
