# Jira CLI — delivery record (RC1-538)

**Decision: drive day-to-day Jira from the REST API via `seed.jira_client.JiraClient`,
exposed as a thin CLI (`uv run python -m seed.jira_cli`), rather than the MCP
connector.** It is cheaper on tokens and more reliable in agent sessions. No
change to the production collector (`collectors/jira.py`) or any deterministic
core; the CLI is dev/ops tooling and stays out of the deployed image.

- **Ticket:** [RC1-538](https://hirereidcollins.atlassian.net/browse/RC1-538),
  under the new DX epic [RC1-537](https://hirereidcollins.atlassian.net/browse/RC1-537).
- **Why it was open:** the MCP Jira tools return fat ADF/field JSON that can't be
  trimmed, so every read spends tokens on payload the task doesn't need; and in
  agent sessions the connector sometimes fails to expose its tools at all, which
  is what forced a raw-curl fallback while shipping RC1-536. REST lets the caller
  set a narrow `fields=` mask and format a compact result, and the existing
  `JiraClient` already handles ADF, transitions and issue creation.

## What shipped

- `seed/jira_cli.py` — subcommands over the existing client:
  - `get KEY [--fields …] [--raw]` — compact issue view (key, type, status,
    parent, assignee, labels) with the ADF description flattened to plain text;
    `--raw` prints the untrimmed JSON. Default mask is a small triage set.
  - `search JQL [--fields …] [--limit N]` — one line per issue (`key | status | summary`).
  - `transition KEY STATUS` — move by status name (`transition_to`).
  - `comment KEY TEXT` — add a comment.
  - `create-story SUMMARY [--parent EPIC] [--desc …] [--label …] [--project RC1]`.
- Reads credentials from `config.settings`; raises cleanly (exit 1) when they are
  absent, so it skips rather than explodes.
- `tests/test_jira_cli.py` — offline, a `FakeJira` wired through the monkeypatched
  `_jira()` factory (the same pattern `simulate` and the collectors use). Covers
  the compact/raw get, the ADF-to-text flattening, search line format + `--limit`,
  the transition success/no-op paths, comment, create-story, and the creds-missing
  error path.

## Placement notes

The CLI lives in `seed/` because that is where `JiraClient` already lives and
`seed/` is not copied into the Dockerfile — the deployed service has no reason to
carry an interactive Jira tool. It is not added to `[tool.setuptools]` packages or
`py-modules`, so the image build is untouched. If the client outgrows the seed
package it can move; the CLI imports it by name and would move with it.

## Dogfood

RC1-537 and RC1-538 were both created with `JiraClient` (epic + parented story),
and this branch's status transitions were driven through the same REST path the
CLI now wraps.
