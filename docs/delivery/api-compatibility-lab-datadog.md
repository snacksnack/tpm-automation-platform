# RC1-613: api-compatibility-lab joins the Datadog estate

Date: 2026-11-04. Early slice of the lab's Datadog integration; the rest
(LLM Obs, DORA, dashboard, SLOs) is RC1-611 and lands with the phases that
create model callers and a deploy path.

What this story wired:

- **CI with Test Optimization** in the lab repo: `ci.yml` per the ESP pattern
  (ruff + `pytest --ddtrace` agentless, `DD_SERVICE: api-compatibility-lab`,
  `DD_ENV: ci`, one run per PR commit, superseded runs cancelled except on
  main). The suite passes identically without `DD_API_KEY` — which also means
  a missing secret looks green while reporting nothing, the RC1-378 gotcha.
- **Service catalog entity** `datadog/entities/api-compatibility-lab.yaml`,
  file-first via `catalog_sync push` before this PR merged. Lifecycle
  `development` on purpose: the scorecard should read the truth, not the
  ambition.
- **CI-failure paging needed no change**: monitor 318355169's query is
  org-wide on `ci_level:pipeline @ci.status:error` (excluding Fly Deploy),
  not repo-scoped, so the lab's pipelines are covered on arrival.
- **Pipeline fingerprints** get added to the entity once the first CI runs
  appear in CI Visibility.

No new billing surface: CI Visibility and Test Optimization bill per active
committer (flat 1 on this account), not per repo.
