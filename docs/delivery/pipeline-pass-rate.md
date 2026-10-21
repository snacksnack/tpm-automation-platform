# Pipeline pass-rate tile scoped to completed runs (RC1-534)

Follow-up to RC1-335. The "Success rate" tile in the Pipeline outcomes group of
`izc-5s7-tz8` computed `success / all_runs`, which let skipped and canceled
no-op runs drag the headline number down and made it disagree with the sibling
"Failed runs" tile.

## The numbers (30 days to 2026-10-21)

Status breakdown, `ci_level:pipeline`, replayed through `/api/v2/query/scalar`:

| Status | Count |
| --- | --- |
| success | 929 |
| error | 33 |
| skipped | 59 |
| canceled | 1 |
| **total** | **1022** |

- Old tile: `929 / 1022` = **90.90%**. The 60 non-error non-success runs (59
  skipped path-filtered Actions workflows + 1 canceled) sat in the denominator,
  so the tile read as if 9% of pipelines were failing when only 33 errored.
- New tile: denominator scoped to completed runs
  (`@ci.status:(success OR error)`), `929 / 962` = **96.57%**. This reconciles
  with "Failed runs" (33 / 962 = 3.43%), which it did not before.
- Tile relabeled **"Pass rate (completed runs)"** so the scope is explicit.

Verified correct, left unchanged:

- **Deploys** (Fly / Heroku / Pages / Deploy): 189 / 191 succeeded = **98.95%**.
- **DORA change failure rate**: 0 failures / 111 deployments = **0.00%**, Elite
  by DORA. Trustworthy only as far as the RC1-448 failure-event instrumentation
  reaches; see `dora-coverage.md`.

The 191 (ci_pipelines deploy runs) vs 111 (DORA deployment events) gap is two
valid lenses, not a defect, and the Deploys group already carries a banner.

## What changed in the account

One widget in the Pipeline outcomes group of `izc-5s7-tz8`: `q0` denominator
scoped to completed runs and the tile title changed. Pushed with
`datadog_sync push` and pulled back to a clean `diff` before merge; the
`datadog-drift.yml` check on the PR stays red until that push lands, which is
the drift job working as designed.
