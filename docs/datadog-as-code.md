# The Datadog account as code

The account is two halves with two different sources, and knowing which half
an object belongs to is the whole of this document.

| | Generated | Exported |
| --- | --- | --- |
| What | 2 program dashboards, 6 KPI monitors, 7 program SLOs | 4 dashboards, 10 monitors, 5 synthetics tests, 2 SLOs |
| Source | `kpi/datadog.py` builds them from the adopted trees | `datadog/*.json`, listed in `datadog/manifest.json` |
| To change one | edit the generator, `python -m kpi.datadog dashboards --push` | edit the file, `python -m kpi.datadog_sync push` |
| Identified by | tag `generated:kpi-datadog`, or the title `Program KPIs — <program>` | absence of the above |

A third half that is nobody's: Datadog's host monitor pack (283790038–45,
tag `monitor_pack:host`) comes with the Agent integration and is left alone.

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

## What stays where it is

`configure_datadog_webhook.py` and `wire_datadog_monitors.py` live in
[ai-incident-summarizer](https://github.com/snacksnack/ai-incident-summarizer)
and stay there: the webhook payload template and the monitor routing are that
service's configuration, already code, already reviewed. They are the reason
`@webhook-incident-summarizer` appears in the monitor messages exported here.

Also out of scope: the Notebooks and RUM application configs, and the GitHub
integration tile's repository filter table, which has no API.
