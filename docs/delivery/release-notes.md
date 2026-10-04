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

Not yet verified: the Slack posts and the reusable workflow, which need the
`#releases` webhook and a deploy from `main`.

## Secrets

| secret | repo | for |
|---|---|---|
| `SLACK_RELEASES_WEBHOOK_URL` | this one, and every repo that calls `release-notify.yml` | both messages |
| `JIRA_EMAIL`, `JIRA_API_TOKEN` | this one | the digest (one Atlassian token serves Jira and Confluence) |

## Rolling out to another repo

1. Add its row to `PATHS` in `kpi/release_notes.py` (the digest).
2. Add the `release-notify` job to its deploy workflow and set the webhook
   secret on the repo (the per-deploy message).

Every deploying repo is public except school-search; the digest reads with
the default Actions token, which cannot see a private repo, so that one needs
a token before its row is added. agent-evals releases on a tag, not on
`main`; `successful_runs` filters on the branch and needs a small change
there, which is also where its GitHub Release text would be written.
