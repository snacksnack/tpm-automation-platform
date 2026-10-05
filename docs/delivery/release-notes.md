# Release notes: a Slack message per deploy, a daily digest in Confluence (RC1-497)

Every production deploy now leaves a record in two places. `#releases` says
what just shipped as each deploy lands; Confluence holds one entry per service
per day, which is the page to read or search later.

## What a release is, and what a note is built from

A release is a deploy run that succeeded. Most of the estate ships on push to
`main`, so there is no tag to hang a note on. The note is the merged pull
requests between the previously deployed sha and this one: the commits in
that range are mapped to their PRs through the GitHub API, and each PR names
its story through the `rc1-NNN-slug` branch (the title is the fallback).
There is no model in the path; the note is a list.

Only successful runs are read. A merge whose deploy failed is in no day's
entry until a later deploy carries it out, and then it is in that day's.

## The two pieces

| piece | where it runs | writes |
|---|---|---|
| `notify` | `release-notify.yml`, a reusable workflow a deploy workflow calls as a job that `needs` its deploy | one `#releases` message |
| `digest` | `release-notes-daily.yml`, 05:23 UTC | the day's section on each service page, then one `#releases` message linking the pages that changed |

Both are `kpi/release_notes.py`. The reusable workflow checks out this repo's
`main`, so the logic has one home and a calling repo carries only the job
stanza (it is in the header of `release-notify.yml`).

A "day" is a day in America/New_York. 05:23 UTC is past local midnight in
both EST and EDT, so yesterday is always complete when the job runs.

Pages live in the RC1 Confluence space: a `Release Notes` parent, one child
per service titled `Release notes: <service>`, newest day first. Both are
created on first use.

## Repeats

- **Re-running a green deploy run** posts nothing: an earlier attempt of the
  same run already succeeded.
- **Redeploying the same sha** posts nothing: the range is empty.
- **Re-running the digest** replaces the day's section. If the page already
  says the same thing it is not written, its version does not move, and
  Slack is not messaged again.
- **Backfilling**: run the workflow by hand with a date. Sections sort by
  date, so a backfilled day lands in order.

## Failure

`notify` never fails its job: the deploy is live by then, and the DORA close
step in `fly-deploy.yml` reads run conclusions. A missing webhook or a failed
post is a warning annotation; the daily digest still records the deploy.
`digest` skips with a warning when the Atlassian secrets are missing and goes
red when a repo or a page could not be read or written.

## Verified live (2026-10-04)

Run from a laptop against the real account, pilot path only:

| day | result |
|---|---|
| 2026-10-01 | page created, 2 deploys, PRs #103 and #104 |
| 2026-10-01 again | "already current", page stayed at version 1 |
| 2026-09-29 | section added below 10-01, PR #102 |
| 2026-09-28 | section added below 09-29, PRs #100 and #101 |
| 2026-09-30, 2026-10-03 | no deploys, nothing written |

Confluence returned the stored body byte-for-byte, which is what the
"already current" check depends on. The 09-29 entry is the 00:27 UTC deploy
of 09-30: the New York day boundary doing its job.

The Slack posts and the reusable workflow were verified the same day by the
merge of the pilot PR itself (#105, deploy run 37201391018), and the digest
from Actions by a manual run for 10-04 (37201672301) and a repeat that
reported "already current" and posted nothing (37201711980).

## The run list can lie, so it is read three times

The first scheduled digest (2026-10-05, for 10-04) wrote four of six
services. hihelloreid and stale-ticket-bot had each deployed one PR the day
before and were reported as "no deploys". Their per-deploy messages had
posted correctly.

The cause is GitHub's list-workflow-runs endpoint. It intermittently answers
200 with a snapshot weeks old: a complete-looking list that ends early.
Caught in a loop of 72 calls: one answer for `fly-deploy.yml` here held 84
runs ending 2026-09-17, for a workflow that had run three times the day
before. The same call was right before and after. Nothing in the status or
headers marks it, and to the digest a stale list is a quiet day.

`successful_runs` now reads the list three times and keeps the longest: a
run list only grows, so the longest read is the newest. At the observed rate
that takes a miss from about 1 in 70 per repo per day to about 1 in 370,000.
The first guess, that `status=success` was the stale part, was wrong: the
unfiltered list did the same thing.

The schedule is also not punctual. The 05:23 UTC run started at 12:20. The
digest is for "yesterday", so lateness costs nothing but the hour it appears.

## Secrets

| secret | repo | for |
|---|---|---|
| `SLACK_RELEASES_WEBHOOK_URL` | this one, and every repo that calls `release-notify.yml` | both messages |
| `JIRA_EMAIL`, `JIRA_API_TOKEN` | this one | the digest (one Atlassian token serves Jira and Confluence) |

## Rollout

| repo | service (page and message name) | deploy workflow | state |
|---|---|---|---|
| tpm-automation-platform | tpm-drift-detector | `fly-deploy.yml` | live 10-04 |
| pr_agent | pr-review-agent-snacksnack | `fly-deploy.yml` | live 10-04 |
| launch-planner-agent | launch-planner-agent | `fly-deploy.yml` | live 10-04 |
| reid_basic | hihelloreid | `heroku-release.yml` | live 10-04 |
| ai-incident-summarizer | incident-summarizer | `deploy.yml` | live 10-04 |
| stale-ticket-bot | stale-ticket-bot | `deploy.yml` | live 10-04 |
| agent-evals | agent-evals | `release.yml` (tag) | wired; first message at the next tag |
| school-search | | `fly-deploy.yml` | left out by decision (10-04) |

"Live" means the repo's own rollout PR deployed and its message arrived in
`#releases`. The rows were dry-run against real history for 09-25 through
10-03 first: every repo read, PRs and stories resolved (09-26, the busiest
day, was 14 deploys and 14 PRs across six repos).

Three things that differ by repo:

- **reid_basic** deploys from a `workflow_run` trigger, where `GITHUB_SHA` is
  main's tip when the run starts, not the sha that shipped. The reusable
  workflow reads `github.event.workflow_run.head_sha` when it is there.
- **ai-incident-summarizer** ships two DORA services (backend and dashboard)
  from one workflow. It gets one page and one message, under the backend's
  name, sent after both jobs pass.
- **agent-evals** is a library: a pushed `v*` tag is the release. Its row is
  `tags=True`, which drops the branch filter and identifies each run by tag
  name instead of sha, so the range is `v0.6.2...v0.6.3`. The tag name is
  used because an annotated tag's run sha can be the tag object, which the
  compare endpoint cannot walk; checked live, `v0.6.1...v0.6.2` (v0.6.1 is
  annotated) returns the five PRs that release carried. Its message reads
  "released" and links the tag. Its `release.yml` also creates the GitHub
  Release with GitHub's generated notes, the same PR list in GitHub's format.

**school-search** stays out. It is private, and the digest reads with the
default Actions token, which cannot see another private repo.

Adding a repo: one row in `PATHS`, the `release-notify` job in its deploy
workflow, and `SLACK_RELEASES_WEBHOOK_URL` set on the repo.

## A coupling to know about

Every calling repo runs `release-notify.yml@main`. A change here that breaks
the workflow's inputs makes every caller's deploy workflow invalid, and an
invalid workflow does not start. Keep `service` and `workflow` as the only
required inputs; add anything new as optional.
