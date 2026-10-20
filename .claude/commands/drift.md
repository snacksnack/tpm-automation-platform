---
description: Datadog-as-code drift check — diff the live account against committed JSON
---
Check for Datadog config drift and report it. Do NOT auto-sync (drift jobs detect; a human commits).

Run from the repo root:
- `uv run python -m kpi.datadog_sync diff` — exit 1 if the live dashboards/monitors/SLOs disagree with the committed JSON; prints a unified diff.
- `uv run python -m kpi.catalog_sync diff` — exit 1 if the catalog has drifted.

If there is drift, show the diff and recommend one of:
- `pull` then commit — to keep an intentional edit made in the Datadog UI, or
- `push` — to undo an unintended account change and restore the committed state.

A human decides and commits. Just surface the diff and the recommendation.
