# The Datadog account as code

The account is two halves with two different sources, and knowing which half
an object belongs to is the whole of this document.

| | Generated | Exported |
| --- | --- | --- |
| What | 2 program dashboards, 6 KPI monitors, 7 program SLOs | 4 dashboards, 11 monitors, 5 synthetics tests, 2 SLOs |
| Source | `kpi/datadog.py` builds them from the adopted trees | `datadog/*.json`, listed in `datadog/manifest.json` |
| To change one | edit the generator, `python -m kpi.datadog dashboards --push` | edit the file, `python -m kpi.datadog_sync push` |
| Identified by | tag `generated:kpi-datadog`, or the title `Program KPIs — <program>` | absence of the above |

A third half that is nobody's: Datadog's host monitor pack (283790038–45,
tag `monitor_pack:host`) comes with the Agent integration and is left alone.

And one table that is neither, because it runs the other way round — see
**The catalog** below.

## Why exported JSON and not Terraform

For one person and ~20 objects, JSON in the repo buys the three things that
actually matter — review, backup, reproducibility — using tools this repo
already has. Terraform would add state to keep somewhere, provider credentials
in CI, and an import step for every existing object; and `terraform import`
starts from an export like this one anyway. Revisit when a second person or a
second account appears (RC1-378).

What this deliberately does **not** do is create objects. The ids in these
files are this account's. Restoring a deleted dashboard into a *fresh* account
would need a create verb, which is out of scope: this is a backup and a review
surface, not a provisioner. Within this account, Datadog's own 30-day
undelete plus a `push` of the file covers the accident that matters.

## The loop

```
python -m kpi.datadog_sync pull    # account -> files
python -m kpi.datadog_sync diff    # exit 1 if they disagree, unified diff
python -m kpi.datadog_sync push    # files -> account
```

`DD_API_KEY` and `DD_APP_KEY` come from `~/.zshrc`, the same single home as
every other credential here (RC1-263).

Two ways to work, both fine:

- **Edit in the UI**, because dragging widgets is faster than writing widget
  JSON — then `pull`, read the diff, commit. This is how the four dashboards
  will keep being built.
- **Edit the file**, for a threshold or a message where the diff *is* the
  review — then `push`.

What is not fine is doing one and forgetting the other, which is what the
drift job is for.

## The drift job

`.github/workflows/datadog-drift.yml` runs `diff` daily at 13:23 UTC (off the
hour, for the reason in `drift-daily.yml`), and on any PR touching `datadog/`.
It needs `DD_API_KEY` and `DD_APP_KEY` as repo secrets and skips cleanly
without them.

A red run is information, not an incident. Someone edited in the UI. Read the
diff, then either `pull` and commit to keep the edit or `push` to undo it.
The job never pushes on its own — a workflow that could rewrite the account
from a branch would make every PR a live change to production observability.

## Things the API taught us

Recorded because each one cost a debugging round, and the stripping rules in
`kpi/datadog_sync.py` exist because of them:

- **The untyped synthetics GET silently omits a browser test's `steps`.**
  `/api/v1/synthetics/tests/{id}` answers for every kind, but only
  `/api/v1/synthetics/tests/browser/{id}` returns the steps — and the steps
  *are* the browser test. Exporting from the untyped endpoint would have
  written a file that, pushed back, deletes both assertions. `_get_synthetic`
  reads the untyped one for the `type` and then re-reads the typed one.
- **A browser test's PUT rejects `public_id`** ("Additional properties are not
  allowed"), though the API test's PUT tolerates it. The files keep it so each
  one names the object it came from; `WRITE_STRIP` drops it on the way out.
- **A step's `public_id` is assigned by Datadog on save**, not chosen — the
  RC1-375 lesson one layer down. Kept in the file, it would read as drift on
  the next pull.
- **Dashboards accept only `team:` and `ai:` tag keys**, so the generated
  dashboards cannot carry `generated:kpi-datadog` the way the generated
  monitors and SLOs do. That is why the guard checks dashboard titles.
- **`options.silenced` is mute state, not configuration.** A downtime would
  otherwise show up as drift every morning it was active.

## The catalog

`datadog/entities/*.yaml` are Software Catalog entities (RC1-447), and they
invert the model above. Nothing in Datadog made them; each one is authored
here and pushed up, so the file is the source rather than an export of one.
That is why they get their own module:

```
python -m kpi.catalog_sync push    # datadog/entities/*.yaml -> account
python -m kpi.catalog_sync diff    # exit 1 if the account has drifted
```

`push` upserts on `metadata.name` and the API answers 202, applying
asynchronously — a `diff` run immediately after a `push` can still read the
old entity for a beat. The ordering rule is the same as everything else here:
**push first, then let the PR go green.**

### One service, three names

A catalog entity is keyed on exactly one name. Every deployed service in this
estate answers to as many as three, and they do not agree:

| Service | DORA deploy | APM `service` | LLM Obs `ml_app` |
| --- | --- | --- | --- |
| drift detector | `tpm-drift-detector` | `drift-service` | `drift-digest` |
| PR review agent | `pr-review-agent-snacksnack` | `webhook` | `pr-review-agent` |
| launch planner | `launch-planner-agent` | *none* | `launch-planner` |
| portfolio site | `hihelloreid` | `web` | `hihelloreid-chat` |

Not one of the four DORA service names appears in APM. The entities are keyed
on the DORA name because that is the deploy identity — the thing a change
failure would attach to — and the other names ride along as `apm-service:` and
`ml-app:` tags so the split is declared rather than rediscovered.

Converging them is deliberately not done here. A Datadog service name is an
identity, not a label: changing `DD_SERVICE` starts a *new* service and leaves
the history behind under the old name, which would split the SLO that RC1-407
built and reset baselines on monitors that have been quiet for weeks. Six
committed objects reference the current APM names, plus the generated KPI
objects and the SLOs.

### What makes the catalog page fill in

An entity with only metadata renders as a row with empty Health, Last Deploy,
Requests, Error Rate, P95 and SLO columns, because every one of those reads
**APM**, keyed on the service name. DORA deployment events do not feed the
catalog's Last Deploy — verified 2026-09-16, when the four DORA-named entities
showed blank and the one APM service in the account showed a deploy.

Two things fill it, at very different prices:

- **`datadog.pipelines.fingerprints` and `codeLocations`**, authored here. Free:
  no service is touched, nothing redeploys. Fingerprints come from the CI
  pipeline event (`ci.pipeline.fingerprint`) and are opaque strings that may
  start with `-` or `_`, so they are quoted in the YAML with the pipeline's
  name in a trailing comment.
- **Making `DD_SERVICE` equal the entity name**, which needs a deploy and
  strands the old service's history.

Only the second fills the performance columns. RC1-447 did it for
`tpm-drift-detector` alone — `DD_SERVICE` in `fly.toml`, stranding 397 spans
nothing queries — as a worked example rather than an estate-wide rename.

### The names that are actually load-bearing

An earlier reading of this put the blast radius at six objects. That was wrong,
and the error is worth recording: a grep for `webhook` and `incident-summarizer`
matched monitor **messages**, where `@webhook-incident-summarizer` is the
notification handle wiring alerts to that service. Those are not service
matches.

Reading the queries instead, the whole account references exactly four service
names: `evals` and `dry-run` (both as *exclusions* in the two fleet-spend
monitors, plus six and five times across the dashboards and SLOs, and three
each in `kpi/datadog.py`), `incident-summarizer` once, and `hihelloreid` once.

So `drift-service` and `webhook` were free to rename and `evals` is expensive.
Check the query, not the document, before pricing a rename.

### Tags that are findings, not names

Two entities carry a `gap:` tag, and both are real:

- `concert-intelligence` has `gap:no-deploy-events`. The n8n workflow JSON is
  re-imported by hand after each merge, so no pipeline and no deploy event
  ever observes it shipping.
That entity used to carry a second `gap:` tag claiming the summarizer's seven
other Lambdas reported under raw CloudFormation names. **That was wrong**, and
it is worth saying why: the claim came from a 30-day APM query, and RC1-411 set
`DD_SERVICE` in the SAM template's `Globals` on 2026-09-10. The raw names were
history inside the window, not live services — a 2-day query returns
`incident-summarizer` alone. Read a window shorter than the age of the fix, or
a closed gap looks open.

The same correction renamed that entity from `ai-incident-summarizer` to
`incident-summarizer`: the shorter name is what APM already emits and what the
site's one `service:incident-summarizer` filter already matches, so the entity
joined its telemetry without touching the service at all. Not every mismatch
needs the service to move; sometimes the entity is the cheaper thing to rename.

`stale-ticket-bot` carries `activity:dormant` for the same reason: it is
deployed, emits nothing, and has no monitor or SLO. Recording that is the
point. An estate where the dormant thing is *declared* dormant is different
from one where nobody checked.

### The scorecard

`kpi/scorecard.py` scores the entities and publishes to Datadog Scorecards,
daily at 13:47 UTC and on any PR touching the rules or the entities:

```
python -m kpi.scorecard show    # evaluate and print, touching nothing
python -m kpi.scorecard push    # create missing rules, then push outcomes
```

**Datadog stores scores; it does not compute them.** The account ships no rules
of its own and a custom rule never evaluates itself — you create the rule, then
POST one outcome per service per rule. The product is the scoreboard; this
module is the scoring. That division is the same one the rest of the repo
keeps: rules decide in Python, nothing is left to a model.

On a pull request the job runs `show`, not `push`. The account's scorecard
should reflect `main`, not whatever a branch proposes.

### The rules, and why one of them starts red

| Rule | Reads | At first push |
| --- | --- | --- |
| Has an owner | entity file | 8/8 |
| Has a repository link | entity file | 8/8 |
| Declares lifecycle and tier | entity file | 8/8 |
| Has a dashboard link | entity file | 8/8 |
| Has an SLO | SLO `service:` tags | **5/8** (2/8 at first push) |

A scorecard whose every rule passes on the day it ships is telling you the
rules are too weak, and that is the first thing a reader will test. `Has an
SLO` went 0/8 → 2/8 in the change that introduced it, by tagging the two site
availability SLOs with the service they actually cover. The other six are real
gaps with tickets behind them, and the number is meant to climb.

### Choosing an SLI the denominator can support (RC1-456)

Three SLOs were added for the services that had none, and the shape differs per
service because the volume does:

- **A ratio needs events.** Only `pr-review-agent-snacksnack` has them — 1,444
  traces in 30 days. The others have 2 to 26, where a 97% target leaves an error
  budget under one event and the SLO measures luck rather than reliability.
- **Scheduled work gets a heartbeat instead**: did it run and succeed, not what
  fraction did. `agent-evals` already had a publish heartbeat monitor;
  `tpm-drift-detector` had no metric at all, so `drift-daily.yml` now reports its
  own success.
- **A CI-pipelines monitor cannot back an SLO.** Datadog rejects it with
  `invalid monitor ids: …, monitors not found or not supported SLO`. That is why
  the drift heartbeat is a metric the workflow posts rather than a monitor over
  the pipeline event that already exists.

**Query on `ml_app`, tag on `service`.** The PR review agent SLO reads
`ml_app:pr-review-agent`, not `service:pr-review-agent-snacksnack`. An SLO's query
and its tags are independent, and RC1-447's rename stranded the history: the new
service name holds **4** traces where the ml_app holds **1,444**. Keyed on the
service it would have been the flappiest SLO in the account. Anything else renamed
needs the same treatment.

**Rules match on tags, never on names.** An SLO counts for a service when it
carries `service:<entity name>`. An earlier estimate of this same rule came
from matching SLO *titles* and put it at 3/8 — wrong in both directions, since
six of the nine SLOs are `generated:kpi-datadog` program SLOs that belong to no
service at all. What an SLO covers is a fact the SLO should state, not
something a scorecard should infer from a string. The same convention will
apply to `Has a monitor` when it lands.

Rules are matched to Datadog by name within the scorecard, so renaming one
creates a new rule and orphans the old — the same trap catalog entities have.
Rename deliberately and delete the old rule by hand.

## What stays where it is

`configure_datadog_webhook.py` and `wire_datadog_monitors.py` live in
[ai-incident-summarizer](https://github.com/snacksnack/ai-incident-summarizer)
and stay there: the webhook payload template and the monitor routing are that
service's configuration, already code, already reviewed. They are the reason
`@webhook-incident-summarizer` appears in the monitor messages exported here.

Also out of scope: the Notebooks and RUM application configs, and the GitHub
integration tile's repository filter table, which has no API. Likewise a
metric's own configuration — the percentile aggregation switched on for the
`pr_agent.review.*` distributions (RC1-395) so p50/p95 can be queried — is
set once through the metrics tag-configuration API and recorded in the
pr_agent decision record, not exported here.

One more thing the API taught us (RC1-394): a dashboard list widget can read
the LLM Observability span stream directly — `data_source:
llm_observability_stream` in a `list_stream` request, accepted by the
dashboard API on 2026-09-06 — so the fleet dashboard's "Last reviews" list
is one row per `pr_review` span with no event emitted per review. The PR
behind a review (`repo`, `pr`, `head_sha`) lives as tags on that span, on
purpose not on the `pr_agent.review.*` metrics: a span tag is free, a metric
tag is a billable custom metric per distinct value, five with percentiles.
