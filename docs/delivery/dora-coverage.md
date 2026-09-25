# DORA coverage, lead-time stages and change failures (RC1-459, RC1-448)

Before RC1-459 four services sent DORA deployment events: the three Fly apps
(RC1-336) and www.hihelloreid.com via Heroku (RC1-354). That was 105 deployments
in the 30 days to 2026-09-21. The other automated deploy paths reached no DORA
data at all.

## Who reports now

| Repo | DORA service | Health gate before the event |
| --- | --- | --- |
| pr_agent | `pr-review-agent-snacksnack` | three consecutive `/healthz` 200s (Fly) |
| tpm-automation-platform | `tpm-drift-detector` | same |
| launch-planner-agent | `launch-planner-agent` | same |
| reid_basic | `hihelloreid` | Heroku release confirmed |
| ai-incident-summarizer | `incident-summarizer` | Datadog webhook without its secret answers 401, three times: the ingest function's own reply |
| ai-incident-summarizer | `incidents-hihelloreid` | `/api/incidents` answers 200 three times: the dashboard reads DynamoDB through its OIDC role |
| stale-ticket-bot | `stale-ticket-bot` | `{"dry_run": true}` invoke: both secrets and the real Jira query, then return before the metric and the Slack post |

The three new services each recorded their first deployment on 2026-09-21
(summarizer `b65fbf6`, stale-ticket-bot `28c6691`). Datadog listed the event
about 90 seconds after the POST returned 200. Don't call an event missing
before that.

**agent-evals** reports each release tag as a deployment (agent-evals#35,
merged 2026-09-22), since consumers pin it by tag. Before reporting,
`release.yml` checks that the tag matches both version strings, then installs
the tag into an empty venv the way a consumer would and imports it. The
trend-page publish is data, not code, and is not counted. The first live event
arrives with the next `v*` tag.

Excluded, by decision (Reid, 2026-09-22):

- **n8n workflows** (concert intelligence, stakeholder status email,
  jira-notion-sync). They ship by importing **and publishing** in the n8n UI
  by hand, so reporting would mean a command run after every publish. A
  forgotten run leaves no record, and missing deploys would make frequency
  look worse and the next deploy's lead time look longer. Leaving them out
  keeps the numbers accurate for the paths that report. Revisit if n8n
  publishing is ever automated.
- **job-search-agent**: a local Claude plugin with nothing deployed.

## Change failures (RC1-448)

Change failure rate and time to restore compute from **DORA failure events**
(`POST /api/v2/dora/failure`, `DD_API_KEY` alone — the API is plain event
ingestion, unaffected by the Incident Management seat wall). RC1-448 built the
pattern on the three Fly workflows; RC1-464/465 extended it to every deploy
path that reports a deployment:

- A deploy whose **boot-health gate fails** opens a failure event with the
  client-chosen id `<service>.<sha>`, `started_at` now, no `finished_at`.
  Only the gate counts: a flyctl or build failure ships nothing, which is a
  failed deployment, not a change failure.
- The next deploy that **passes** the gate closes every failure since the
  previous success by re-POSTing each id with `finished_at` set. The API
  **upserts by id and replaces the record wholesale** (verified 2026-09-25),
  so the close re-sends the failed run's own gate timestamp as `started_at`,
  read from the GitHub jobs API — including earlier attempts of a run that
  was re-run to green, which `gh run list` hides. Each candidate run is
  checked for a failed gate step first, because POSTing an id that was never
  opened would fabricate a completed failure.
- A wrong event is recoverable: `DELETE /api/v2/dora/failure/{id}` works with
  the API + application keys (also verified; both probe events were removed).

Per-service gates (RC1-464/465): the three Fly boot gates; the summarizer's
two — ingest-401 for `incident-summarizer`, `/api/incidents`-200 for
`incidents-hihelloreid`, one workflow whose close walk verifies each job's
gate so the services close independently; stale-ticket-bot's dry-run invoke;
agent-evals' release gates (a broken tag standing as latest — that one
measures **broken-release exposure, not downtime**, since consumers pin by
tag; read its TTR accordingly); and `hihelloreid`'s serve-gate, added by
RC1-465, which also made its deployment event serve-gated like Fly's. The
mechanics live in `scripts/report_dora_failure.sh` and
`scripts/close_dora_failures.sh`, byte-identical across the four non-Fly
repos — RC1-448's inline steps generalized with the workflow filename
parameterized, multiple gate-step names, and no branch filter (a release
workflow's runs live on tag refs).

The only paths not emitting are the n8n workflows, which are excluded from
DORA entirely.

Two facts that bound the data. The API incident source was enabled
**2026-09-25** and events whose `started_at` predates the flip are rejected,
so no failure can ever be backfilled — the four monitor-318355170 failures
from early September are unrecordable, and the first CFR data point arrives with
the first real gate failure. And the RC1-448 parking comment's ~4.7% CFR
estimate overstated: at least pr_agent's 09-12 failure was flyctl dying with
the gate **skipped** (run 34694038761 attempt 1), a failed deployment that
under the gate definition never counted.

The DORA group on `izc-5s7-tz8` gained a change-failure row (CFR %, time to
restore avg, failures by service), each query replayed through
`/api/v2/query/scalar` against a probe event before the widget was written
(`indexes: ["failure"]`, TTR metric `time_to_restore`, seconds).

## Lead time, by stage

Every deployment event carries `averaged_metrics` for three stages, and the
`commit` index answers each as its own metric:

| Metric | Stage |
| --- | --- |
| `time_to_pr_ready` | first commit → PR opened (or out of draft) |
| `merge_time` | PR opened → merged: the time spent in PR |
| `time_to_deploy` | merged → deployment finished |
| `review_time` | accepted by the query API, always empty here |

`review_time` needs a GitHub review, and PRs in this estate are merged
without a formal approval, so `merge_time` covers the whole PR phase.

Past month, p50 by service, as the new table on `izc-5s7-tz8` reads it:

| Service | commit → PR | in PR p50 | in PR p75 | merge → deploy | total p50 |
| --- | --- | --- | --- | --- | --- |
| tpm-drift-detector | 0.1m | 12.6m | 33.4m | 0.2m | 2.7h |
| pr-review-agent-snacksnack | 0.1m | 7.0m | 19.1m | 0.2m | 15.4m |
| hihelloreid | 0.1m | 5.0m | 13.5m | 0.1m | 7.9m |
| launch-planner-agent | 0.0m | 3.0m | 4.8m | 152.1h | 152.1h |

Two traps in reading it:

- **The stages do not add up to the total.** Each column is its own
  percentile, and commits pushed straight to `main` have a lead time but no
  PR stages. tpm-drift-detector's 2.7h total p50, against 12.6m in PR and
  seconds on either side, puts most of its lead time outside the three stages.
  Which commits carry it has not been checked.
- **launch-planner's 152h is waiting to deploy, not time in PR.** The commits
  merged and then sat for about a week until the next deploy.

Every widget query was replayed through `/api/v2/query/scalar` (or
`/timeseries`) before the widget was written: data source `dora`, index
`commit`, `compute.metric` set to the stage name. The numbers above are that
replay.

## What changed in the account

Two widgets and a rewritten note in the DORA group of `izc-5s7-tz8`, pushed
before this PR and pulled back (`datadog_sync diff` clean at 36 objects), and
a corrected description on the `incident-summarizer` catalog entity
(`catalog_sync diff` clean at 8).
