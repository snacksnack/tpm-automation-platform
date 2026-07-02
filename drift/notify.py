"""Route the drift digest to the right people (RC1-139 [8/9]).

- Weekly rollup: the full digest to the program channel via incoming webhook.
- Per-owner alerts: a message to the downstream ticket's assignee, but only for
  NEW red findings (or a red finding that escalated / resolved). Dedup is backed
  by the store so an owner is alerted once at red, not every run.
- Jira assignee -> Slack user via a config map; unmapped owners fall back to a
  channel mention rather than crashing.
- DRY_RUN logs instead of sending (development + demos).

Transport is abstracted behind a Sender so the routing/dedup logic is unit-tested
with no network. An incoming webhook can only post to one channel, so it cannot
DM (supports_dm = False) — those alerts fall back to channel mentions until a bot
token is wired up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from collectors.models import ProjectSnapshot
from narrative.models import DriftDigest
from store.models import Finding
from store.snapshot_store import SnapshotStore

logger = logging.getLogger(__name__)


class NotifyError(RuntimeError):
    pass


@dataclass
class NotifyConfig:
    # Jira assignee (accountId or display name) -> Slack user id/handle.
    slack_user_map: dict[str, str] = field(default_factory=dict)


@dataclass
class Alert:
    kind: str  # "new_red" | "escalated" | "resolved"
    finding: Finding


@dataclass
class NotifyResult:
    rollup_sent: bool
    dm_count: int
    mention_count: int


# --------------------------------------------------------------------------- #
# Senders
# --------------------------------------------------------------------------- #
class DryRunSender:
    """Logs instead of sending. Default when DRY_RUN or no webhook configured."""

    supports_dm = True

    def channel(self, text: str) -> None:
        logger.info("[DRY_RUN] channel post:\n%s", text)

    def dm(self, slack_user: str, text: str) -> None:
        logger.info("[DRY_RUN] DM -> %s:\n%s", slack_user, text)


class SlackWebhookSender:
    """Posts to one channel via an incoming webhook. Cannot DM."""

    supports_dm = False

    def __init__(self, webhook_url: str, *, timeout: float = 15.0):
        self._url = webhook_url
        self._timeout = timeout

    def channel(self, text: str) -> None:
        r = httpx.post(self._url, json={"text": text}, timeout=self._timeout)
        if r.status_code >= 300:
            raise NotifyError(f"Slack webhook -> HTTP {r.status_code}: {r.text[:200]}")

    def dm(self, slack_user: str, text: str) -> None:  # pragma: no cover - guarded by supports_dm
        raise NotifyError("incoming webhook cannot DM; map is unused without a bot token")


def make_sender(*, dry_run: bool, webhook_url: str | None):
    if dry_run or not webhook_url:
        return DryRunSender()
    return SlackWebhookSender(webhook_url)


# --------------------------------------------------------------------------- #
# Dedup + routing
# --------------------------------------------------------------------------- #
def compute_alerts(
    findings: list[Finding], store: SnapshotStore, project_key: str, run_id: int
) -> list[Alert]:
    """New/escalated red findings to alert on, plus reds that resolved since last run."""
    prev_run = store.previous_run_id(project_key, run_id)
    prior = {f.identity: f for f in (store.get_findings(prev_run) if prev_run else [])}
    current_ids = {f.identity for f in findings}

    alerts: list[Alert] = []
    for f in findings:
        if f.severity_bucket != "red":
            continue
        p = prior.get(f.identity)
        if p is None:
            alerts.append(Alert("new_red", f))  # first time seen at all
        elif p.severity_bucket != "red" or f.severity > p.severity:
            alerts.append(Alert("escalated", f))  # became red, or worse than before
        # else: already alerted at red with no increase -> stay quiet
    for p in prior.values():
        if p.severity_bucket == "red" and p.identity not in current_ids:
            alerts.append(Alert("resolved", p))
    return alerts


def render_rollup(digest: DriftDigest) -> str:
    if digest.all_clear:
        return f"*{digest.subject}*\n{digest.summary}"
    parts = [f"*{digest.subject}*", "", digest.summary, ""]
    parts.extend(line.line for line in digest.findings)
    return "\n".join(parts)


def render_alert(alert: Alert) -> str:
    f = alert.finding
    if alert.kind == "resolved":
        return f"✅ Resolved — {f.downstream} ({f.rule_type}) is no longer drifting."
    if alert.kind == "escalated":
        head = f"🔴 Escalated (severity {f.severity})"
    else:
        head = "🔴 New collision risk"
    return f"{head} — {f.downstream}: {f.detail}"


def _owner(finding: Finding, snapshot: ProjectSnapshot) -> tuple[str | None, str | None]:
    issue = snapshot.by_key.get(finding.downstream)
    return (issue.assignee_name, issue.assignee_id) if issue else (None, None)


def send_notifications(
    digest: DriftDigest,
    findings: list[Finding],
    snapshot: ProjectSnapshot,
    store: SnapshotStore,
    run_id: int,
    *,
    config: NotifyConfig,
    sender,
) -> NotifyResult:
    """Post the channel rollup, then route per-owner alerts (DM or mention)."""
    sender.channel(render_rollup(digest))

    dm_count = mention_count = 0
    for alert in compute_alerts(findings, store, snapshot.project_key, run_id):
        name, account = _owner(alert.finding, snapshot)
        slack_user = config.slack_user_map.get(account or "")
        if not slack_user:
            slack_user = config.slack_user_map.get(name or "")
        text = render_alert(alert)
        if slack_user and sender.supports_dm:
            sender.dm(slack_user, text)
            dm_count += 1
        else:  # unmapped assignee or DM-less transport -> channel mention, never crash
            sender.channel(f"(owner: {name or 'unassigned'}) {text}")
            mention_count += 1

    return NotifyResult(rollup_sent=True, dm_count=dm_count, mention_count=mention_count)
