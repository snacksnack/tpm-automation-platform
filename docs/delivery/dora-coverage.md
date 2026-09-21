# DORA coverage and lead-time stages (RC1-459)

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

Not reporting, and why:

- **agent-evals**: will report (Reid, 2026-09-21). Each release tag is the
  deployment, since consumers pin it by tag. The trend-page publish is data,
  not code, and is excluded.
- **n8n workflows** (concert, stakeholder email, jira-notion-sync): shipped
  by importing and publishing in the n8n UI by hand. Undecided.
- **job-search-agent**: a local Claude plugin with nothing deployed.

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
