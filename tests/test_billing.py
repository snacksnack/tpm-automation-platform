"""Real billing feeds and the real-cost-per-run KPI — offline (RC1-308).

The readers are tested against canned HTTP responses (the documented shapes
of the Anthropic cost report and the Heroku invoice list), the collector's
wiring against the same health rules every other source obeys, the store
round-trip against SQLite, and the measure against hand-built snapshots.
Nothing here talks to a network or needs a key.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from collectors import billing, programs
from collectors import program as collect
from collectors.models import BillingRow, EvalRunRow, ProgramSnapshot, SourceHealth
from kpi import catalog, measures
from store.snapshot_store import SnapshotStore

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
TODAY = NOW.date()
EVAL = programs.get("eval-run-store")


# --- the readers -----------------------------------------------------------------------------


def _resp(payload, status=200):
    return SimpleNamespace(
        status_code=status, json=lambda: payload, text=str(payload)[:300]
    )


def test_anthropic_costs_sums_buckets_paginates_and_keeps_zero_days(monkeypatch):
    pages = [
        {
            "data": [
                {
                    "starting_at": "2026-08-23T00:00:00Z",
                    "ending_at": "2026-08-24T00:00:00Z",
                    "results": [{"amount": "123.45", "currency": "USD"},
                                {"amount": "76.55", "currency": "USD"}],
                },
                {
                    "starting_at": "2026-08-24T00:00:00Z",
                    "ending_at": "2026-08-25T00:00:00Z",
                    "results": [],
                },
            ],
            "has_more": True,
            "next_page": "page_abc",
        },
        {
            "data": [
                {
                    "starting_at": "2026-08-25T00:00:00Z",
                    "ending_at": "2026-08-26T00:00:00Z",
                    "results": [{"amount": "50", "currency": "USD"}],
                }
            ],
            "has_more": False,
            "next_page": None,
        },
    ]
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(dict(params))
        return _resp(pages[len(calls) - 1])

    monkeypatch.setattr(billing.httpx, "get", fake_get)
    rows = billing.read_anthropic_costs("sk-ant-admin-x", now=NOW)
    assert [(r.period_start.isoformat(), r.amount_usd) for r in rows] == [
        ("2026-08-23", 2.0),   # 200.00 cents
        ("2026-08-24", 0.0),   # a zero-spend day is a measurement, not a gap
        ("2026-08-25", 0.5),
    ]
    assert all(r.source == "anthropic-costs" and r.kind == "metered" for r in rows)
    assert calls[0]["bucket_width"] == "1d" and "page" not in calls[0]
    assert calls[1]["page"] == "page_abc"


def test_heroku_invoices_are_cents_and_a_bad_shape_raises(monkeypatch):
    monkeypatch.setattr(
        billing.httpx, "get",
        lambda *a, **k: _resp([
            {"period_start": "2026-07-01", "period_end": "2026-07-31", "total": 500,
             "state": 1},
            {"period_start": "2026-08-01", "period_end": "2026-08-31", "total": 500,
             "state": 0},
        ]),
    )
    rows = billing.read_heroku_invoices("key")
    assert [r.amount_usd for r in rows] == [5.0, 5.0]
    assert rows[0].kind == "invoice" and rows[0].source == "heroku-invoices"

    monkeypatch.setattr(billing.httpx, "get", lambda *a, **k: _resp([{"nope": 1}]))
    with pytest.raises(billing.BillingError, match="malformed invoice"):
        billing.read_heroku_invoices("key")


def test_an_http_error_is_a_billing_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(billing.httpx, "get", lambda *a, **k: _resp({}, status=401))
    with pytest.raises(billing.BillingError, match="HTTP 401"):
        billing.read_anthropic_costs("bad-key", now=NOW)


# --- collector wiring ------------------------------------------------------------------------


def test_missing_keys_are_error_health_rows_never_empty_bills():
    snap = collect.collect_program(EVAL, jira=None, eval_dsn=None, now=NOW)
    by_source = {h.source: h for h in snap.health}
    assert by_source["anthropic-costs"].status == "error"
    assert "ANTHROPIC_ADMIN_KEY" in by_source["anthropic-costs"].detail
    assert by_source["heroku-invoices"].status == "error"
    assert "HEROKU_API_KEY" in by_source["heroku-invoices"].detail
    assert snap.billing == []


def test_billing_rows_collect_and_round_trip_through_the_store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        billing, "read_anthropic_costs",
        lambda key, now=None: [
            BillingRow(source="anthropic-costs", period_start=TODAY - timedelta(days=1),
                       period_end=TODAY, amount_usd=1.25)
        ],
    )
    monkeypatch.setattr(
        billing, "read_heroku_invoices",
        lambda key: [
            BillingRow(source="heroku-invoices", period_start=date(2026, 8, 1),
                       period_end=date(2026, 8, 31), amount_usd=5.0, kind="invoice")
        ],
    )
    snap = collect.collect_program(
        EVAL, jira=None, eval_dsn=None, heroku_api_key="h", anthropic_admin_key="a", now=NOW
    )
    by_source = {h.source: h for h in snap.health}
    assert by_source["anthropic-costs"].status == "ok"
    assert by_source["heroku-invoices"].status == "ok"
    assert len(snap.billing) == 2

    with SnapshotStore(tmp_path / "s.db") as store:
        run_id = store.save_program_snapshot(snap, project_key=EVAL.project_key)
        loaded = store.load_program_snapshot(run_id)
    assert loaded.billing == sorted(snap.billing, key=lambda r: (r.source, r.period_start))


# --- the measure -----------------------------------------------------------------------------


def _run(days_ago=1, cost=0.10):
    return EvalRunRow(
        run_id=f"r{days_ago}", subject="s", code_version="1", model="m",
        started_at=datetime.combine(TODAY - timedelta(days=days_ago),
                                    datetime.min.time(), UTC),
        cases=10, passed=9, errored=0, cost_usd=cost,
    )


def _snap(*, metered_usd=2.0, metered_age=0, runs=(1, 2), run_cost=0.10,
          invoice=True, health_ok=True, today=TODAY):
    rows = []
    if metered_usd is not None:
        for back in range(3):
            day = today - timedelta(days=metered_age + back + 1)
            rows.append(BillingRow(
                source="anthropic-costs", period_start=day,
                period_end=day + timedelta(days=1),
                amount_usd=metered_usd / 3,
            ))
    if invoice:
        rows.append(BillingRow(
            source="heroku-invoices", period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 29), amount_usd=5.0, kind="invoice",
        ))
    status = "ok" if health_ok else "error"
    return ProgramSnapshot(
        program_id="eval-run-store", collected_at=NOW, sim_date=today,
        eval_runs=[_run(d, cost=run_cost) for d in runs],
        billing=rows,
        health=[
            SourceHealth(source="eval-store", status="ok", count=len(runs)),
            SourceHealth(source="anthropic-costs", status=status,
                         detail="" if health_ok else "HTTP 401"),
            SourceHealth(source="heroku-invoices", status=status,
                         detail="" if health_ok else "HTTP 401"),
        ],
    )


def test_real_cost_per_run_is_billed_dollars_over_runs():
    r = measures.real_cost_per_run(EVAL, [_snap()])
    # $2 metered + $5/28d invoice prorated to 28d = $7 over 2 runs.
    assert r.value == pytest.approx(3.5)
    assert r.state == "ok" and not r.tripped
    assert "attribution $0.20" in r.detail and "upper bound" in r.detail


def test_real_cost_reads_broken_when_a_feed_errors_or_answers_nothing():
    r = measures.real_cost_per_run(EVAL, [_snap(health_ok=False)])
    assert r.state == "broken" and "anthropic-costs unreadable" in r.reason
    r = measures.real_cost_per_run(EVAL, [_snap(invoice=False)])
    assert r.state == "broken" and "heroku-invoices" in r.reason
    gone = measures.source_missing(_snap())
    r = measures.real_cost_per_run(EVAL, [gone])
    assert r.state == "broken" and r.value is None


def test_a_quiet_month_is_broken_with_the_money_named_never_zero():
    r = measures.real_cost_per_run(EVAL, [_snap(runs=(40, 45))])
    assert r.state == "broken" and r.value is None
    assert "$2.00 of real model spend with nothing measured" in r.reason


def test_real_cost_goes_stale_when_the_feed_stops_landing():
    r = measures.real_cost_per_run(EVAL, [_snap(metered_age=4)])
    assert r.state == "stale" and "cost-report bucket" in r.reason


def test_the_gap_trips_only_on_two_consecutive_readings():
    # $7 real vs $0.02 attributed: ratio far over 3 on both days.
    wide = [_snap(run_cost=0.01, today=TODAY - timedelta(days=1)),
            _snap(run_cost=0.01)]
    assert measures.real_cost_per_run(EVAL, wide).tripped
    # First reading over the line does not trip alone.
    assert not measures.real_cost_per_run(EVAL, [_snap(run_cost=0.01)]).tripped
    # And a healthy ratio never trips.
    assert not measures.real_cost_per_run(EVAL, wide[:1] + [_snap(run_cost=5.0)]).tripped


# --- the catalog -----------------------------------------------------------------------------


def test_catalog_lists_billing_for_the_real_program_only():
    evl = catalog.catalog(EVAL)
    assert any(s["name"] == "billing" for s in evl["sources"])
    assert "billing.amount_usd" in evl["field_names"]
    assert not any("Heroku billing" in line for line in evl["not_available"]["other"])
    assert "billing" in evl["not_available"]  # attribution limits, stated

    sim = catalog.catalog(programs.get("simulated-program"))
    assert not any(s["name"] == "billing" for s in sim["sources"])
    assert any("Heroku billing" in line for line in sim["not_available"]["other"])
