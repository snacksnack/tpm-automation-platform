"""Capture canned Jira fixtures from live RC1 for the collector tests.

Run once (live) after the demo scenario is seeded; the JSON it writes into
tests/fixtures/ is what the unit tests parse, so the tests never hit the API.
Re-run to refresh fixtures after re-seeding.

    python -m scripts.capture_fixtures

Reads keys from seed/manifest.json so it stays in sync with the seeded scenario.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from collectors.jira import ISSUE_FIELDS
from config import settings

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
MANIFEST = Path(__file__).resolve().parent.parent / "seed" / "manifest.json"


def _key_for(manifest: dict, slug: str) -> str:
    return next(t["key"] for t in manifest["tickets"] if t["slug"] == slug)


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST.read_text())
    slip_key = _key_for(manifest, "slip-up")  # has a duedate change
    nodate_key = _key_for(manifest, "inv-down")  # priority/link changes, no date change

    api = f"{settings.jira_base_url.rstrip('/')}/rest/api/3"
    with httpx.Client(auth=(settings.jira_email, settings.jira_api_token), timeout=30.0) as c:
        search = c.post(
            f"{api}/search/jql",
            json={
                "jql": 'project = RC1 AND labels = "drift-demo" ORDER BY key ASC',
                "fields": ISSUE_FIELDS,
                "maxResults": 100,
            },
        ).json()
        _write("rc1_search_drift_demo.json", search)

        _write("rc1_changelog_slip.json", c.get(f"{api}/issue/{slip_key}/changelog").json())
        _write("rc1_changelog_nodates.json", c.get(f"{api}/issue/{nodate_key}/changelog").json())

    print(f"slip-up={slip_key}  inv-down(no-date)={nodate_key}")


def _write(name: str, payload: object) -> None:
    (FIXTURES / name).write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote tests/fixtures/{name}")


if __name__ == "__main__":
    main()
