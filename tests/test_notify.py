"""Notifier tests — dedup + routing with no network (RC1-139 acceptance)."""

from __future__ import annotations

import logging

import pytest

from collectors.models import Issue, ProjectSnapshot
from drift.notify import (
    DryRunSender,
    NotifyConfig,
    SlackWebhookSender,
    make_sender,
    send_notifications,
)
from narrative.models import DigestLine, DriftDigest
from store.models import Finding
from store.snapshot_store import SnapshotStore


class _FakeSender:
    supports_dm = True

    def __init__(self):
        self.channels: list[str] = []
        self.dms: list[tuple[str, str]] = []

    def channel(self, text: str) -> None:
        self.channels.append(text)

    def dm(self, slack_user: str, text: str) -> None:
        self.dms.append((slack_user, text))


def _issue(key: str, owner: str | None, account: str | None) -> Issue:
    return Issue(
        key=key, summary=f"S {key}", status="In Progress", status_category="In Progress",
        assignee_name=owner, assignee_id=account,
    )


SNAPSHOT = ProjectSnapshot(
    project_key="RC1",
    issues=[
        _issue("RC1-2", "Dana", "A-DANA"),
        _issue("RC1-9", "Kim", "A-KIM"),
        _issue("RC1-3", None, None),  # unassigned
    ],
)
MAPPED = NotifyConfig(slack_user_map={"A-DANA": "U-DANA", "A-KIM": "U-KIM"})
DIGEST = DriftDigest(
    subject="RC1 drift",
    summary="2 red.",
    findings=[DigestLine(downstream="RC1-2", bucket="red", line="x")],
)


def _red(down: str, sev: float, rule: str = "unabsorbed_slip") -> Finding:
    return Finding(rule_type=rule, downstream=down, upstream="RC1-1", severity=sev,
                   severity_bucket="red", detail=f"{down} drift")


def _white(down: str) -> Finding:
    return Finding(rule_type="transitive_risk", downstream=down, upstream="RC1-1",
                   severity=2.0, severity_bucket="white", detail=f"{down} watch")


@pytest.fixture
def store() -> SnapshotStore:
    s = SnapshotStore(":memory:")
    yield s
    s.close()


def test_first_run_rollup_plus_dms_for_new_reds(store: SnapshotStore):
    findings = [_red("RC1-2", 28.0), _red("RC1-9", 20.0), _white("RC1-3")]
    r1 = store.create_run("RC1")
    saved = store.save_findings(r1, findings)

    sender = _FakeSender()
    result = send_notifications(DIGEST, saved, SNAPSHOT, store, r1, config=MAPPED, sender=sender)

    assert result.rollup_sent and result.dm_count == 2 and result.mention_count == 0
    assert len(sender.channels) == 1  # rollup only; both reds went to DMs
    assert {u for u, _ in sender.dms} == {"U-DANA", "U-KIM"}


def test_second_run_no_change_sends_rollup_zero_repeat_dms(store: SnapshotStore):
    findings = [_red("RC1-2", 28.0), _red("RC1-9", 20.0)]
    r1 = store.create_run("RC1")
    store.save_findings(r1, findings)
    send_notifications(DIGEST, findings, SNAPSHOT, store, r1, config=MAPPED, sender=_FakeSender())

    r2 = store.create_run("RC1")
    saved2 = store.save_findings(r2, findings)  # identical
    sender = _FakeSender()
    result = send_notifications(DIGEST, saved2, SNAPSHOT, store, r2, config=MAPPED, sender=sender)

    assert result.dm_count == 0 and result.mention_count == 0
    assert len(sender.channels) == 1  # rollup still goes out


def test_unmapped_assignee_falls_back_to_channel_mention(store: SnapshotStore):
    findings = [_red("RC1-2", 28.0), _red("RC1-3", 25.0)]  # RC1-3 is unassigned
    r1 = store.create_run("RC1")
    saved = store.save_findings(r1, findings)

    sender = _FakeSender()
    # Empty map -> everyone unmapped -> mentions, no crash.
    result = send_notifications(
        DIGEST, saved, SNAPSHOT, store, r1, config=NotifyConfig(), sender=sender
    )

    assert result.dm_count == 0 and result.mention_count == 2
    assert len(sender.channels) == 3  # rollup + 2 mentions
    assert any("owner: unassigned" in c for c in sender.channels)


def test_escalation_realerts(store: SnapshotStore):
    r1 = store.create_run("RC1")
    store.save_findings(r1, [_red("RC1-2", 20.0)])
    send_notifications(
        DIGEST, [_red("RC1-2", 20.0)], SNAPSHOT, store, r1, config=MAPPED, sender=_FakeSender()
    )

    r2 = store.create_run("RC1")
    worse = store.save_findings(r2, [_red("RC1-2", 40.0)])  # severity increased
    sender = _FakeSender()
    result = send_notifications(DIGEST, worse, SNAPSHOT, store, r2, config=MAPPED, sender=sender)

    assert result.dm_count == 1
    assert "Escalated" in sender.dms[0][1]


def test_resolved_red_produces_alert(store: SnapshotStore):
    r1 = store.create_run("RC1")
    store.save_findings(r1, [_red("RC1-2", 28.0)])
    send_notifications(
        DIGEST, [_red("RC1-2", 28.0)], SNAPSHOT, store, r1, config=MAPPED, sender=_FakeSender()
    )

    r2 = store.create_run("RC1")
    store.save_findings(r2, [])  # the finding cleared
    clear = DriftDigest(subject="RC1 all clear", summary="No drift.", all_clear=True)
    sender = _FakeSender()
    result = send_notifications(clear, [], SNAPSHOT, store, r2, config=MAPPED, sender=sender)

    assert result.dm_count == 1
    assert "Resolved" in sender.dms[0][1]


def test_make_sender_defaults_to_dry_run():
    assert isinstance(make_sender(dry_run=True, webhook_url="https://x"), DryRunSender)
    assert isinstance(make_sender(dry_run=False, webhook_url=None), DryRunSender)
    assert isinstance(make_sender(dry_run=False, webhook_url="https://x"), SlackWebhookSender)


def test_dry_run_sender_logs(caplog):
    with caplog.at_level(logging.INFO):
        DryRunSender().channel("hello")
    assert "DRY_RUN" in caplog.text
