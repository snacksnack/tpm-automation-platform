"""Snapshot store tests — temp DB per test, no shared state (RC1-135 acceptance)."""

from __future__ import annotations

from datetime import date

import pytest

from collectors.models import DependencyLink, Issue, ProjectSnapshot
from store.models import Finding
from store.snapshot_store import SnapshotStore


@pytest.fixture
def store() -> SnapshotStore:
    s = SnapshotStore(":memory:")  # fresh temp DB per test
    yield s
    s.close()


def _issue(key: str, *, due: date | None = None, start: date | None = None,
           status: str = "In Progress", category: str = "In Progress") -> Issue:
    return Issue(
        key=key, summary=f"Summary {key}", status=status, status_category=category,
        priority="High", assignee_id="acc-1", assignee_name="Reid", due=due, start=start,
    )


def _snapshot(issues: list[Issue], links: list[tuple[str, str]] | None = None) -> ProjectSnapshot:
    return ProjectSnapshot(
        project_key="RC1",
        issues=issues,
        links=[DependencyLink(upstream=u, downstream=d) for u, d in (links or [])],
    )


def test_load_previous_none_when_empty(store: SnapshotStore):
    assert store.load_previous("RC1") is None


def test_save_and_reload_roundtrip(store: SnapshotStore):
    snap = _snapshot(
        [_issue("RC1-1", due=date(2026, 7, 20)), _issue("RC1-2", start=date(2026, 7, 8))],
        links=[("RC1-1", "RC1-2")],
    )
    run = store.create_run("RC1")
    store.save_snapshot(run, snap)

    loaded = store.load_previous("RC1")
    assert loaded is not None
    assert {i.key for i in loaded.issues} == {"RC1-1", "RC1-2"}
    assert loaded.by_key["RC1-1"].due == date(2026, 7, 20)
    assert loaded.by_key["RC1-2"].start == date(2026, 7, 8)
    assert loaded.links == [DependencyLink(upstream="RC1-1", downstream="RC1-2")]
    # date_changes are not persisted — reconstructed issues carry an empty list.
    assert loaded.by_key["RC1-1"].date_changes == []


def test_two_runs_are_queryable_before_and_after(store: SnapshotStore):
    r1 = store.create_run("RC1")
    store.save_snapshot(r1, _snapshot([_issue("RC1-1", due=date(2026, 7, 10))]))
    r2 = store.create_run("RC1")
    store.save_snapshot(r2, _snapshot([_issue("RC1-1", due=date(2026, 7, 20))]))  # slipped

    before = store.load_previous("RC1", before_run=r2)
    after = store.load_previous("RC1")
    assert before.by_key["RC1-1"].due == date(2026, 7, 10)
    assert after.by_key["RC1-1"].due == date(2026, 7, 20)  # the drift is observable across runs


def test_append_only_keeps_every_run(store: SnapshotStore):
    for _ in range(3):
        r = store.create_run("RC1")
        store.save_snapshot(r, _snapshot([_issue("RC1-1")]))
    rows = store._conn.execute("SELECT COUNT(*) AS n FROM issue_snapshots").fetchone()["n"]
    assert rows == 3  # nothing overwritten


def test_finding_first_seen_carried_forward(store: SnapshotStore):
    f = Finding(rule_type="unabsorbed_slip", upstream="RC1-1", downstream="RC1-2",
                severity=9.0, severity_bucket="red")

    r1 = store.create_run("RC1")
    saved1 = store.save_findings(r1, [f])
    assert saved1[0].first_seen_run == r1
    assert saved1[0].is_new is True

    r2 = store.create_run("RC1")
    g = Finding(
        rule_type="lead_time_risk", downstream="RC1-9", severity=3.0, severity_bucket="white"
    )
    saved2 = store.save_findings(r2, [f, g])
    by_rule = {x.rule_type: x for x in saved2}
    assert by_rule["unabsorbed_slip"].first_seen_run == r1  # carried over, not re-stamped
    assert by_rule["unabsorbed_slip"].is_new is False
    assert by_rule["lead_time_risk"].first_seen_run == r2  # genuinely new
    assert by_rule["lead_time_risk"].is_new is True


def test_finding_first_seen_handles_null_upstream(store: SnapshotStore):
    f = Finding(
        rule_type="lead_time_risk", downstream="RC1-9", severity=2.0, severity_bucket="white"
    )
    r1 = store.create_run("RC1")
    store.save_findings(r1, [f])
    r2 = store.create_run("RC1")
    saved = store.save_findings(r2, [f])
    assert saved[0].first_seen_run == r1  # NULL upstream still matches by identity


def test_get_findings_returns_typed_sorted(store: SnapshotStore):
    r = store.create_run("RC1")
    store.save_findings(r, [
        Finding(rule_type="a", downstream="RC1-1", severity=1.0, severity_bucket="white"),
        Finding(rule_type="b", downstream="RC1-2", severity=8.0, severity_bucket="red"),
    ])
    got = store.get_findings(r)
    assert all(isinstance(x, Finding) for x in got)
    assert [x.severity for x in got] == [8.0, 1.0]  # severity DESC


def test_file_backed_persists_across_reopen(tmp_path):
    db = tmp_path / "drift.db"
    with SnapshotStore(db) as s:
        run = s.create_run("RC1")
        s.save_snapshot(run, _snapshot([_issue("RC1-1", due=date(2026, 7, 20))]))
    with SnapshotStore(db) as s2:  # reopen the same file
        loaded = s2.load_previous("RC1")
        assert loaded.by_key["RC1-1"].due == date(2026, 7, 20)
