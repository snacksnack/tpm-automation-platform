# Metrics store — the decision (RC1-304)

**Decision: the KPI readings land in Postgres on `reid-eval-store`, not in
Datadog.** Datadog's free plan retains metrics for **one day**; the epic needs a
weekly KPI read back over ten-plus weeks, which is seventy-odd days. The store
fails the requirement by roughly seventy times, and the fourteen-day trial fails
it by five, so no amount of care with the submission API buys the thing the
track stage actually needs. Buying the retention means Infrastructure Pro at $15
per host per month for fifteen months of history — history the Heroku Postgres
behind the eval-run store already keeps for nothing, on a plan with no row
limit, in a database this repo can already reach and write to. The metric values
are computed deterministically by code from snapshots (RC1-301) and were never
going to be computed by the metrics layer; what the layer owes us is durable
storage and a picture. We already own the storage. Grafana Cloud's free tier
supplies the picture by reading that Postgres directly over its built-in SQL
data source, which sidesteps Grafana's own fourteen-day cap because that cap
applies to its metrics store and not to a database it queries.

Time-boxed to one evening as the ticket asked; the retention finding settled it
early, so the rest of the evening went on verifying the fallback rather than on
resolving Datadog questions that could no longer change the answer.

## The three questions

**1. Does the free (or trial) plan accept custom metrics via the API, and at
what volume?** Unresolved, and deliberately so. Datadog documents custom-metric
allotments only for paid tiers — 100 ingested and 100 indexed per host on Pro,
200 on Enterprise — and the plan-comparison page does not list a free
infrastructure tier at all. Whether submission from a free account is silently
accepted or refused is not stated anywhere in the docs. It stopped mattering
once question 2 came back the way it did: a store that accepts writes but
forgets them within a day cannot serve a ten-week trend, so the answer would not
have moved the decision either way.

**2. Retention: can a weekly KPI be read back over 10+ weeks?** **No.** The
pricing page gives the free plan "up to 5 hosts" and "**1 day metric
retention**", against fifteen months on Pro. The 14-day trial is full-featured
and needs no credit card, but ten weeks of weekly readings take seventy days to
accumulate, so the trial window closes five times over before the third weekly
brief that RC1-306 and RC1-308 both require. This is the finding the decision
rests on.

**3. Can a dashboard be shared or screenshotted for the portfolio without a paid
seat?** Yes, but not usefully on Datadog — a dashboard over a one-day window has
no trend on it to show. On the chosen path, Grafana Cloud's forever-free tier
gives three users and a built-in PostgreSQL data source that needs no plugin and
connects to managed external databases. Whether Grafana's *public* dashboard
link is free-tier is not confirmed in their docs; if it turns out to be gated,
the portfolio artifact is a screenshot or a static published page, which is what
the epic asked for anyway ("shared **or** screenshotted").

## The fallback, verified

Checked live from the repo venv on 2026-08-24 rather than taken on trust:

| check | result |
| --- | --- |
| reachable | connected via `psycopg2` with `sslmode=require` |
| engine | PostgreSQL 18.3 (Heroku Essential tier, Amazon Aurora) |
| privilege | `CREATE` held on the database — the track stage can make its own table |
| current size | 8,417 kB, one application table (`eval_runs`, 60 rows) |
| row limit | none; Heroku's Essential-tier plans dropped row caps |

Retention on this path is "as long as we keep the rows", which is the property
Datadog wanted $15 per host per month for.

Credentials keep their single home: `EVAL_DATABASE_URL` in `~/.zshrc` and
nowhere else (RC1-263), pulled out of the profile by `scripts/kpi_daily.sh`
because launchd does not read shell profiles. Nothing new to configure.

## What this leaves for the track stage (RC1-305)

Decided here: **Postgres on `reid-eval-store`, Grafana Cloud free tier for the
dashboard.** Still open, and properly RC1-305's to settle:

- Whether the `kpi_readings` table lives in that Postgres alongside `eval_runs`
  or in the snapshot store (`data/drift.db`). The snapshots are SQLite on a Fly
  volume; the readings want to be where the dashboard can reach them, which
  argues for Postgres, but that is a design call with its own trade-off and it
  belongs in the ticket that builds the writer.
- The reading schema — at minimum program, kpi id, sim-date, value, state, and
  the run the reading came from, so that every number in a brief traces back to
  its snapshot (RC1-306's done-criteria).
- Staleness as a first-class state in that schema: a KPI whose source is
  missing is written `stale`, never `0`. The collector already reports per-source
  health this way (`docs/kpi/snapshots.md`); the readings table must not throw
  that away at the last step.

## Sources

- Datadog pricing (free plan hosts and retention): https://www.datadoghq.com/pricing/
- Datadog custom-metrics billing (per-tier allotments): https://docs.datadoghq.com/account_management/billing/custom_metrics/
- Datadog plan comparison (no free infrastructure tier listed): https://www.datadoghq.com/pricing/list/
- Datadog trial terms (14 days, no card): https://www.datadoghq.com/free-datadog-trial/
- Grafana Cloud pricing (free tier limits): https://grafana.com/pricing/
- Grafana PostgreSQL data source (built in, external managed databases): https://grafana.com/docs/grafana-cloud/connect-externally-hosted/data-sources/postgres/
- Heroku Postgres Essential plans (no row limits): https://devcenter.heroku.com/changelog-items/2877
