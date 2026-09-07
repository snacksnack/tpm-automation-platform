# The conventions file — measurement record (RC1-399)

**Decision: a one-page `CLAUDE.md` at the repo root, under 6,000 characters,
so the PR review agent's context builder finds the repository's conventions
without sending a model to look for them.** No agent code changed. This is
the third repository to get the file after the two n8n ones (RC1-396); the
alternative there — a router rule that treats callers plus tests as enough
context — was declined again for the same reason: the file costs nothing in
the agent, and the rule would change every repository's review.

- **Ticket:** [RC1-399](https://hirereidcollins.atlassian.net/browse/RC1-399)
- **Why it was open:** under the RC1-393/394 rule the review context is
  complete only when conventions, callers and tests are all answered by
  Python. With no conventions file the context was never complete, so the
  scout ran to its cap on every platform PR. RC1-397's two live reviews of
  #64 (9.8 ¢ and 10.5 ¢, 47–50 s) paid for that.
- **Method:** `pr_agent/scripts/measure_pr.py 64 --multi --verify
  --repo-dir ../tpm-automation-platform [--overlay CLAUDE.md]` — the same PR
  at the same head (7a306af), once as it was and once with the working-tree
  file laid over the worktree, a 5.5-minute gap between them so the second
  run could not read the first's prompt cache (the after-run's warm call
  wrote 5,370 tokens to cache and read 0, which is the proof). No Datadog
  keys in the shell, so neither run appears on the fleet dashboard. Two
  billed reviews, 14 ¢.

## Result

| Run | Context | Scout | Cost | Wall | Findings |
| --- | --- | --- | --- | --- | --- |
| #64 as merged, no file | `conventions=None`, incomplete | ran, 7 turns, 7.6 ¢ (72 %) | **10.6 ¢** | 56.3 s | 0 |
| #64 + `CLAUDE.md` (5,959 chars) | `conventions=CLAUDE.md`, complete | skipped | **3.6 ¢** | 8.8 s | 0 |

Two thirds off the cost and six times faster, for a one-file JSON change with
nothing to find. The reviewer stages cost the same in both runs (1.4–1.6 ¢
across the three); the whole difference is the scout, replaced by a warm
call that is 0.4 ¢ dearer because the prefix now carries the page.

## What the page says, and why that shape

Six sections in the order the reviewer needs them: what the repository is,
the layout, the conventions a change is held to, testing, commands, the
per-ticket workflow. The conventions are the ones the code already lives by
— rules decide and the model narrates, imports at the top, no auto-sync of
infrastructure state, Datadog objects as code, config through settings,
ddtrace as a main dependency, singular metric names, explicit source
health, skip-clean-fail-loud in scheduled jobs — written as prose so a
reviewer does not have to grep for them. Nothing in it is new policy.

The cap matters: `context.select_sections` keeps a file at or under 6,000
characters whole and otherwise cuts it to the conventions, testing and
layout sections first. The first draft was 6,226 characters and was cut;
the committed page is 5,959. Keep it there — a longer page reaches the
reviewers as a shorter one.

## What to watch

- The first live code PR after merge should log `context conventions=CLAUDE.md
  … complete=True scout_turns=0` and land a `mode:multi` point in the
  5–10 ¢ band on the fleet dashboard's last-reviews list. This PR itself is
  documentation-only and is routed without the scout regardless, so it is
  not the check.
- `n8n-jira-notion-sync` is now the only repository without a file; it has
  never had a PR.
