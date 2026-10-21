# PR agent keep-warm moved to a Datadog synthetic (RC1-535)

The PR review agent (`pr_agent`) is a scale-to-zero Fly app; a review job
interrupted by a crash or stop sits in its durable SQLite queue until the
machine is next started. RC1-423 kept it warm with
`pr_agent/.github/workflows/wake.yml`, a `*/15` GitHub cron pinging `/healthz`.

## Why it moved

GitHub throttles high-frequency `schedule` events. Measured over the 30 days to
2026-10-21: the cron was scheduled 96×/day but GitHub actually ran it **168
times (~5.6/day)**, so the keep-warm fired about once every ~4 hours, not every
15 minutes. (Datadog CI Visibility ingested 164/168 ≈ 98%, so the Delivery
dashboard counts were accurate — the gap was GitHub, not telemetry.) A tighter
cron cannot fix this; GitHub drops the extra runs regardless, so the trigger
had to move off GitHub.

## What replaced it

A Datadog Synthetic HTTP test, `69w-d3w-zv2`, created via the API and adopted as
code (the sync module is a backup/review surface, not a provisioner, so a new
object is created once by API then pulled — same path the other five synthetics
took):

- GET `https://pr-review-agent-snacksnack.fly.dev/healthz` from `aws:us-east-1`,
  `tick_every` 900s, assert status 200 only (no latency assertion, so a
  cold-start wake does not false-fail), 2 retries.
- Datadog runs synthetics on a reliable schedule, and the inbound request starts
  the Fly machine, whose lifespan re-queues interrupted jobs and drains the
  queue — the same mechanism the cron used.
- Doubles as the uptime monitor the PR agent lacked. Alert monitor `335370262`
  is auto-created and owned by the test (manifest `push: false`); it notifies
  `@webhook-incident-summarizer` at priority 4, so only a genuine unreachable
  service pages, not a cold start.

Verified on creation: two on-demand runs, the first **3.5s (cold start — it woke
the stopped machine)**, the second **33ms (warm)**, both passed.

`datadog_sync diff` clean at 40 objects. The GitHub `wake.yml` cron is removed in
the paired `pr_agent` change.
