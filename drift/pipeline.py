"""End-to-end drift run (RC1-140 [9/9]).

collect -> graph -> rules -> store -> narrate -> notify, emitting one structured
JSON log line per run (counts per rule, duration, notification outcomes). Invoked
by POST /drift/run and by `python -m drift.pipeline`.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date
from pathlib import Path

from collectors.jira import JiraCollector
from config import settings
from drift.graph import DependencyGraph
from drift.notify import NotifyConfig, make_sender, send_notifications
from drift.rules import detect_all
from narrative.drift_digest import build_digest
from store.models import Finding
from store.snapshot_store import SnapshotStore

logger = logging.getLogger("drift.run")


def run_drift(
    *,
    project_key: str | None = None,
    jql: str | None = None,
    today: date | None = None,
    dry_run: bool | None = None,
) -> dict:
    """Run one drift detection cycle and return a structured summary."""
    project_key = project_key or settings.project_key
    today = today or date.today()
    dry_run = settings.dry_run if dry_run is None else dry_run
    started = time.perf_counter()

    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    store = SnapshotStore(settings.db_path)
    try:
        with JiraCollector(
            settings.jira_base_url, settings.jira_email or "", settings.jira_api_token or ""
        ) as collector:
            snapshot = collector.collect(project_key, jql=jql)

        run_id = store.create_run(project_key)
        # Prior snapshot loaded before saving this one — rule 4's "since last run".
        previous = store.load_previous(project_key, before_run=run_id)
        store.save_snapshot(run_id, snapshot)

        graph = DependencyGraph.from_snapshot(snapshot)
        findings = detect_all(graph, snapshot, previous=previous, today=today)
        saved = store.save_findings(run_id, findings)

        digest = build_digest(saved, snapshot)
        sender = make_sender(dry_run=dry_run, webhook_url=settings.slack_webhook_url)
        notify = send_notifications(
            digest, saved, snapshot, store, run_id, config=NotifyConfig(), sender=sender
        )

        summary = _summary(project_key, run_id, snapshot, saved, notify, started, dry_run)
    finally:
        store.close()

    logger.info(json.dumps(summary))
    return summary


def _summary(project, run_id, snapshot, findings: list[Finding], notify, started, dry_run) -> dict:
    by_rule: dict[str, int] = {}
    by_bucket = {"red": 0, "yellow": 0, "white": 0}
    for f in findings:
        by_rule[f.rule_type] = by_rule.get(f.rule_type, 0) + 1
        by_bucket[f.severity_bucket] = by_bucket.get(f.severity_bucket, 0) + 1
    return {
        "event": "drift_run",
        "project": project,
        "run_id": run_id,
        "issues": len(snapshot.issues),
        "links": len(snapshot.links),
        "findings": len(findings),
        "by_rule": by_rule,
        "by_bucket": by_bucket,
        "notify": {
            "rollup": notify.rollup_sent,
            "dms": notify.dm_count,
            "mentions": notify.mention_count,
        },
        "dry_run": dry_run,
        "duration_ms": round((time.perf_counter() - started) * 1000),
    }


if __name__ == "__main__":  # `python -m drift.pipeline` — cron-without-HTTP option
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(json.dumps(run_drift(), indent=2))
