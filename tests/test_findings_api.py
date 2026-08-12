"""Read-only findings endpoints (RC1-244).

The reason these exist is that `POST /drift/run` is not a read: it collects from
Jira, writes rows, calls Anthropic, and posts to Slack. So the load-bearing
tests here are the negative ones — that reading findings does none of that, no
matter how many times a caller asks.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import main
from store.models import Finding
from store.snapshot_store import SnapshotStore

client = TestClient(main.app)

RED = Finding(
    rule_type="timeline_inversion",
    upstream="RC1-157",
    downstream="RC1-158",
    severity=9.1,
    severity_bucket="red",
    detail="RC1-157 due 2026-07-20 lands after RC1-158 start/due 2026-07-08 (12d overlap).",
)
YELLOW = Finding(
    rule_type="lead_time_risk",
    upstream="RC1-161",
    downstream="RC1-162",
    severity=4.0,
    severity_bucket="yellow",
    detail="RC1-161 not started; RC1-162 starts 2026-07-04.",
)
NO_UPSTREAM = Finding(
    rule_type="orphan_risk",
    upstream=None,
    downstream="RC1-170",
    severity=2.0,
    severity_bucket="white",
    detail="RC1-170 has no upstream link.",
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Point the app at an empty temp database."""
    path = str(tmp_path / "drift.db")
    monkeypatch.setattr(main.settings, "db_path", path)
    monkeypatch.setattr(main.settings, "project_key", "RC1")
    return path


def _seed(path: str, findings: list[Finding], project_key: str = "RC1") -> int:
    store = SnapshotStore(path)
    try:
        run_id = store.create_run(project_key, created_at=datetime.now(UTC))
        store.save_findings(run_id, findings)
        return run_id
    finally:
        store.close()


def _runs(path: str) -> int:
    store = SnapshotStore(path)
    try:
        return store._conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
    finally:
        store.close()


# --- the read is genuinely a read -------------------------------------------


def test_reading_findings_never_runs_the_pipeline(db, monkeypatch):
    """The whole reason this endpoint exists. `run_drift` collects from Jira,
    writes rows, calls Anthropic and posts to Slack — a reader must reach none
    of it."""
    _seed(db, [RED, YELLOW])

    monkeypatch.setattr(
        main, "run_drift", lambda **kw: pytest.fail("reading findings ran the pipeline")
    )
    import drift.notify as notify

    monkeypatch.setattr(
        notify,
        "send_notifications",
        lambda *a, **kw: pytest.fail("reading findings sent a notification"),
    )

    for _ in range(10):
        assert client.get("/drift/findings").status_code == 200


def test_ten_reads_create_no_new_runs(db):
    _seed(db, [RED])
    before = _runs(db)
    for _ in range(10):
        client.get("/drift/findings")
    assert _runs(db) == before == 1


def test_reading_makes_no_anthropic_call(db, monkeypatch):
    """The narrative module imports the SDK lazily, so an accidental call would
    show up as an import. Fail loudly if the read path touches it."""
    _seed(db, [RED])
    import narrative.drift_digest as digest

    monkeypatch.setattr(
        digest, "build_digest", lambda *a, **kw: pytest.fail("reading findings built a digest")
    )
    assert client.get("/drift/findings").status_code == 200


# --- the empty state is a state, not an error -------------------------------


def test_no_run_yet_returns_an_empty_list_and_a_null_run(db):
    """The scheduler may simply not have fired. That is not a 500, and it is not
    'no drift' either — the null run id is what says which."""
    body = client.get("/drift/findings").json()
    assert body["run_id"] is None
    assert body["run_at"] is None
    assert body["count"] == 0
    assert body["findings"] == []


# --- reading the latest run -------------------------------------------------


def test_findings_come_from_the_most_recent_run(db):
    _seed(db, [RED])
    second = _seed(db, [RED, YELLOW])

    body = client.get("/drift/findings").json()
    assert body["run_id"] == second
    assert body["count"] == 2


def test_every_response_names_the_run_it_came_from(db):
    run_id = _seed(db, [RED])
    body = client.get("/drift/findings").json()
    assert body["run_id"] == run_id
    assert body["run_at"]
    assert body["project_key"] == "RC1"


def test_detail_is_returned_verbatim(db):
    """It carries the dates and the triggering change already; parsing it into
    fields would be fragile for no gain."""
    _seed(db, [RED])
    finding = client.get("/drift/findings").json()["findings"][0]
    assert finding["detail"] == RED.detail


def test_carried_findings_are_marked_not_new(db):
    _seed(db, [RED])
    _seed(db, [RED])
    finding = client.get("/drift/findings").json()["findings"][0]
    assert finding["first_seen_run"] == 1
    assert finding["is_new"] is False


# --- filters ----------------------------------------------------------------


def test_bucket_filter(db):
    _seed(db, [RED, YELLOW, NO_UPSTREAM])
    body = client.get("/drift/findings", params={"bucket": "red"}).json()
    assert body["count"] == 1
    assert body["findings"][0]["severity_bucket"] == "red"


def test_an_invalid_bucket_is_rejected(db):
    _seed(db, [RED])
    resp = client.get("/drift/findings", params={"bucket": "purple"})
    assert resp.status_code == 422
    assert "red" in resp.json()["detail"]


def test_rule_filter(db):
    _seed(db, [RED, YELLOW])
    body = client.get("/drift/findings", params={"rule": "lead_time_risk"}).json()
    assert body["count"] == 1
    assert body["findings"][0]["rule_type"] == "lead_time_risk"


def test_since_run_filter_selects_newly_seen_findings(db):
    _seed(db, [RED])
    second = _seed(db, [RED, YELLOW])

    body = client.get("/drift/findings", params={"since_run": second}).json()
    assert [f["rule_type"] for f in body["findings"]] == ["lead_time_risk"]


# --- addressing one finding by identity -------------------------------------


def test_a_finding_is_addressed_by_its_identity(db):
    _seed(db, [RED, YELLOW])
    body = client.get(
        f"/drift/findings/{RED.rule_type}/{RED.downstream}",
        params={"upstream": RED.upstream},
    ).json()
    assert body["count"] == 1
    assert body["findings"][0]["detail"] == RED.detail


def test_the_identity_still_resolves_after_a_later_run(db):
    """The reason this is not a row id. Findings are re-derived every run, so a
    caller that listed findings and then asked about one would get a different
    finding — or none — as soon as the scheduler fired again."""
    _seed(db, [RED, YELLOW])
    url = f"/drift/findings/{RED.rule_type}/{RED.downstream}"
    params = {"upstream": RED.upstream}
    first = client.get(url, params=params).json()

    _seed(db, [RED, YELLOW, NO_UPSTREAM])  # the scheduler fires again
    second = client.get(url, params=params).json()

    assert second["findings"][0]["detail"] == first["findings"][0]["detail"]
    assert second["run_id"] > first["run_id"]


def test_a_rule_with_no_upstream_is_addressable(db):
    _seed(db, [NO_UPSTREAM])
    body = client.get(
        f"/drift/findings/{NO_UPSTREAM.rule_type}/{NO_UPSTREAM.downstream}"
    ).json()
    assert body["count"] == 1
    assert body["findings"][0]["upstream"] is None


def test_omitting_a_required_upstream_does_not_match(db):
    """`(rule, None, downstream)` and `(rule, RC1-157, downstream)` are different
    findings. Matching loosely would return someone else's."""
    _seed(db, [RED])
    resp = client.get(f"/drift/findings/{RED.rule_type}/{RED.downstream}")
    assert resp.status_code == 404


def test_an_unknown_finding_says_where_to_look(db):
    _seed(db, [RED])
    resp = client.get("/drift/findings/timeline_inversion/RC1-999")
    assert resp.status_code == 404
    assert "/drift/findings" in resp.json()["detail"]


def test_explaining_a_finding_when_nothing_has_run(db):
    resp = client.get("/drift/findings/timeline_inversion/RC1-158")
    assert resp.status_code == 404


# --- the write path is untouched --------------------------------------------


def test_drift_run_still_requires_its_token(db, monkeypatch):
    """RC1-244 must not change the scheduler's endpoint."""
    monkeypatch.setattr(main.settings, "drift_run_token", "s3cret")
    monkeypatch.setattr(main, "run_drift", lambda **kw: {"ok": True})

    assert client.post("/drift/run").status_code == 401
    assert client.post("/drift/run", headers={"X-Drift-Token": "s3cret"}).status_code == 200


def test_the_read_endpoints_need_no_token(db):
    """They expose nothing a Jira reader could not already see, and gating them
    would just push callers back to the endpoint that notifies."""
    _seed(db, [RED])
    assert client.get("/drift/findings").status_code == 200
