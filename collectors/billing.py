"""Real billing feeds: dollars as billed, not as computed (RC1-308).

Two readers, one per feed the eval-run-store program declares:

- **anthropic-costs** — the org's Cost Report
  (`GET /v1/organizations/cost_report`, Admin API key): daily buckets of
  actual billed model spend, amounts in cents as decimal strings. With a
  `workspace_id` (RC1-327: eval traffic runs on the dedicated `agent-evals`
  workspace since 2026-08-29), the report is grouped by workspace and the
  rows are scoped to that workspace's spend exactly — the org-wide total
  still ships beside them as `anthropic-costs-org`, because fleet economics
  (RC1-322) is a different question with the same feed. Without one, the
  old org-wide behaviour stands and a KPI reading it states that
  attribution caveat rather than hiding it.
- **heroku-invoices** — the account's invoices
  (`GET /account/invoices`, Platform API v3): one row per monthly billing
  period, amounts in cents. The store plan's real bill, replacing the
  `store_plan_usd_per_month` declared constant.

Both raise `BillingError` on any failure; the collector turns that into an
`error` health row, exactly as it does for Jira and the eval store — a feed
that could not be read is absent and says so, never an empty list
pretending to be a $0 bill.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx

from collectors.models import BillingRow

ANTHROPIC_COST_URL = "https://api.anthropic.com/v1/organizations/cost_report"
HEROKU_INVOICES_URL = "https://api.heroku.com/account/invoices"

#: How far back one snapshot reaches. The KPI window is 28 days; a few extra
#: days mean a late-landing bucket still enters the window it belongs to.
COST_REPORT_DAYS = 35
_TIMEOUT = 30.0


class BillingError(RuntimeError):
    pass


def _get(url: str, *, headers: dict, params: dict | None = None) -> httpx.Response:
    try:
        resp = httpx.get(url, headers=headers, params=params, timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        raise BillingError(f"{url}: {type(exc).__name__}: {exc}") from exc
    if resp.status_code >= 300:
        raise BillingError(f"{url} -> HTTP {resp.status_code}: {resp.text[:200]}")
    return resp


def read_anthropic_costs(
    admin_key: str,
    *,
    workspace_id: str | None = None,
    days: int = COST_REPORT_DAYS,
    now: datetime | None = None,
) -> list[BillingRow]:
    """Daily billed model spend, oldest first.

    With `workspace_id`, `anthropic-costs` rows carry that workspace's spend
    exactly and `anthropic-costs-org` rows carry the org total beside them;
    without one, `anthropic-costs` is the org total, as before. Zero-spend
    days are kept as $0 rows either way: a day nothing was spent is a
    measurement, distinct from a day the feed could not be read.
    """
    now = now or datetime.now(UTC)
    headers = {"x-api-key": admin_key, "anthropic-version": "2023-06-01"}
    params: dict = {
        "starting_at": (now - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z"),
        "bucket_width": "1d",
        "limit": 31,  # the endpoint's maximum page size
    }
    if workspace_id:
        # The default workspace's results carry workspace_id None, so the
        # eval scope is an exact match, never a remainder.
        params["group_by[]"] = "workspace_id"

    def row(source: str, bucket: dict, cents: Decimal) -> BillingRow:
        return BillingRow(
            source=source,
            period_start=date.fromisoformat(bucket["starting_at"][:10]),
            period_end=date.fromisoformat(bucket["ending_at"][:10]),
            amount_usd=float(cents / 100),
            kind="metered",
        )

    rows: list[BillingRow] = []
    while True:
        data = _get(ANTHROPIC_COST_URL, headers=headers, params=params).json()
        for bucket in data["data"]:
            total = sum(Decimal(r["amount"]) for r in bucket["results"])
            if workspace_id:
                scoped = sum(
                    Decimal(r["amount"])
                    for r in bucket["results"]
                    if r.get("workspace_id") == workspace_id
                )
                rows.append(row("anthropic-costs", bucket, scoped))
                rows.append(row("anthropic-costs-org", bucket, total))
            else:
                rows.append(row("anthropic-costs", bucket, total))
        if not data.get("has_more"):
            return sorted(rows, key=lambda r: (r.period_start, r.source))
        params["page"] = data["next_page"]


def read_heroku_invoices(api_key: str) -> list[BillingRow]:
    """The account's monthly invoices, oldest first. Amounts are cents."""
    headers = {
        "Accept": "application/vnd.heroku+json; version=3",
        "Authorization": f"Bearer {api_key}",
    }
    invoices = _get(HEROKU_INVOICES_URL, headers=headers).json()
    rows = []
    for inv in invoices:
        try:
            rows.append(
                BillingRow(
                    source="heroku-invoices",
                    period_start=date.fromisoformat(str(inv["period_start"])[:10]),
                    period_end=date.fromisoformat(str(inv["period_end"])[:10]),
                    amount_usd=float(inv["total"]) / 100,
                    kind="invoice",
                )
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise BillingError(f"malformed invoice from Heroku: {exc}: {str(inv)[:200]}") from exc
    return sorted(rows, key=lambda r: r.period_start)
