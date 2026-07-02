"""Seed the RC1 drift-demo scenario (RC1-133 [2/9]).

Idempotent: tickets are matched by their ``ds-<slug>`` label, so re-running
converges instead of duplicating. The slip and the Blocked transition are
applied last, generating the changelog history rules 2 and 4 depend on.

Usage:
    python -m seed.seed_demo              # create / converge the scenario
    python -m seed.seed_demo --dry-run    # show what would happen, no writes
    python -m seed.seed_demo --teardown   # delete every drift-demo ticket

Requires JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN in the environment / .env.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from config import settings
from seed import scenario
from seed.jira_client import JiraClient, JiraError

DEMO_LABEL = "drift-demo"
MANIFEST = Path(__file__).with_name("manifest.json")


def slug_label(slug: str) -> str:
    return f"ds-{slug}"


def find_existing(jira: JiraClient) -> dict[str, str]:
    """Map slug -> issue key for tickets already seeded (by ds-<slug> label)."""
    issues = jira.search(f'project = RC1 AND labels = "{DEMO_LABEL}"', ["labels"])
    found: dict[str, str] = {}
    for issue in issues:
        for label in issue["fields"].get("labels", []):
            if label.startswith("ds-"):
                found[label[3:]] = issue["key"]
    return found


def teardown(jira: JiraClient, *, dry_run: bool) -> None:
    existing = find_existing(jira)
    if not existing:
        print("Nothing to tear down — no drift-demo tickets found.")
        return
    for slug, key in sorted(existing.items()):
        if dry_run:
            print(f"[dry-run] would delete {key} ({slug})")
        else:
            jira.delete_issue(key)
            print(f"deleted {key} ({slug})")
    if not dry_run and MANIFEST.exists():
        MANIFEST.unlink()
        print(f"removed {MANIFEST.name}")


def seed(jira: JiraClient, *, dry_run: bool) -> None:
    account_id = None if dry_run else jira.my_account_id()
    keys = find_existing(jira)

    # 1. create any missing tickets (with create-time dates + labels)
    for t in scenario.TICKETS:
        if t.slug in keys:
            print(f"exists  {keys[t.slug]:>8}  {t.slug}")
            continue
        labels = [DEMO_LABEL, slug_label(t.slug)]
        if dry_run:
            print(f"[dry-run] create  {t.slug}: '{t.summary}' due={t.due} start={t.start}")
            keys[t.slug] = f"<{t.slug}>"
            continue
        key = jira.create_story(
            t.summary, labels, due=t.due, start=t.start, assignee_id=account_id
        )
        keys[t.slug] = key
        print(f"created {key:>8}  {t.slug}")

    # 2. priorities (not on the create screen — set via PUT)
    for t in scenario.TICKETS:
        if dry_run:
            print(f"[dry-run] set priority {t.slug} -> {t.priority}")
        else:
            jira.set_priority(keys[t.slug], t.priority)

    # 3. links: blocker blocks blocked
    for blocker, blocked in scenario.links():
        if dry_run:
            print(f"[dry-run] link  {blocker} blocks {blocked}")
            continue
        if keys[blocked] in jira.outward_blocks(keys[blocker]):
            print(f"link ok  {blocker} blocks {blocked}")
        else:
            jira.create_blocks_link(keys[blocker], keys[blocked])
            print(f"linked   {blocker} blocks {blocked}")

    # 4. statuses (transitions) — includes moving trans-a into Blocked
    for t in scenario.TICKETS:
        if t.status is None:
            continue
        if dry_run:
            print(f"[dry-run] transition {t.slug} -> {t.status}")
        elif jira.transition_to(keys[t.slug], t.status):
            print(f"moved    {keys[t.slug]:>8}  {t.slug} -> {t.status}")
        else:
            print(f"status ok {keys[t.slug]:>7}  {t.slug} already {t.status}")

    # 5. slip LAST — so the changelog entry postdates the downstream date-set
    for t in scenario.TICKETS:
        if not t.slip_due_to:
            continue
        if dry_run:
            print(f"[dry-run] slip  {t.slug} due {t.due} -> {t.slip_due_to}")
        elif jira.duedate(keys[t.slug]) == t.slip_due_to:
            print(f"slip ok  {t.slug} already at {t.slip_due_to}")
        else:
            jira.set_duedate(keys[t.slug], t.slip_due_to)
            print(f"slipped  {t.slug} due -> {t.slip_due_to}")

    # 6. verify one link direction (epic warns this is easy to get backwards)
    if not dry_run:
        inv_up, inv_down = keys["inv-up"], keys["inv-down"]
        if inv_down in jira.outward_blocks(inv_up):
            print(f"verify   OK: {inv_up} outward-blocks {inv_down}")
        else:
            print(f"verify   WARN: expected {inv_up} to block {inv_down} — check direction!")

    write_manifest(keys, dry_run=dry_run)


def write_manifest(keys: dict[str, str], *, dry_run: bool) -> None:
    manifest = {
        "project": "RC1",
        "today_reference": "2026-07-01",
        "demo_label": DEMO_LABEL,
        "tickets": [
            {
                "slug": t.slug,
                "key": keys.get(t.slug),
                "summary": t.summary,
                "rule": t.rule,
                "role": t.role,
                "priority": t.priority,
                "start": t.start,
                "due": t.slip_due_to or t.due,
                "due_initial": t.due if t.slip_due_to else None,
                "status": t.status or "Idea (default, not started)",
                "blocks": [keys.get(b, b) for b in t.blocks],
                "note": t.note,
            }
            for t in scenario.TICKETS
        ],
        "expected_findings": scenario.expected_findings(),
    }
    if dry_run:
        print(f"\n[dry-run] would write {MANIFEST.name} with {len(manifest['tickets'])} tickets")
        return
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"\nwrote {MANIFEST.name}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed the RC1 drift-demo scenario.")
    ap.add_argument("--dry-run", action="store_true", help="print actions, make no changes")
    ap.add_argument("--teardown", action="store_true", help="delete all drift-demo tickets")
    args = ap.parse_args()

    try:
        with JiraClient(
            settings.jira_base_url, settings.jira_email or "", settings.jira_api_token or ""
        ) as jira:
            if args.teardown:
                teardown(jira, dry_run=args.dry_run)
            else:
                seed(jira, dry_run=args.dry_run)
    except JiraError as e:
        raise SystemExit(f"Jira error: {e}") from e


if __name__ == "__main__":
    main()
