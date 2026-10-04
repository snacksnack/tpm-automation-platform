"""Release notes: a Slack message per deploy, a daily digest in Confluence (RC1-497).

A release here is a production deploy that succeeded — most of the estate
ships on push to `main`, so there is no tag to hang a note on. The note is
built from merged pull requests, not a diff: the commits between the
previously deployed sha and this one are mapped back to the PRs that carried
them, and each PR names its story through the `rc1-NNN-slug` branch.

Two commands, deliberately independent:

    notify   run by a deploy workflow after the deploy succeeds (through the
             reusable `release-notify.yml`). One message to #releases naming
             the PRs that just shipped. A notification, not the record.
    digest   run once a day. One entry per service per day on that service's
             Confluence page, newest day first, then one #releases message
             linking the pages that changed. This is the record.

Only successful deploy runs are read, so a merge whose deploy failed is not
reported as shipped: it appears on the day a later deploy carries it out.

The digest is a scheduled job that writes to Confluence. That is publishing
a report, the same class as the drift digest going to Slack — it is not the
infrastructure-state sync the repo forbids scheduled jobs from doing.

Run: `python -m kpi.release_notes notify --repo R --service S --workflow W
     --sha SHA --run-id ID [--run-attempt N] [--dry-run]`
     `python -m kpi.release_notes digest [--date YYYY-MM-DD] [--dry-run]`
Env (through `config.settings`): `GITHUB_TOKEN`, `SLACK_RELEASES_WEBHOOK_URL`,
and for the digest `JIRA_EMAIL` + `JIRA_API_TOKEN` (one Atlassian token
serves Jira and Confluence).
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from config import settings

OWNER = "snacksnack"
GITHUB_API = "https://api.github.com"
DEFAULT_BRANCH = "main"

#: A "day" of releases is a day where Reid works, not a UTC day: an evening
#: session would otherwise split across two entries.
LOCAL_TZ = ZoneInfo("America/New_York")

SPACE_KEY = "RC1"
PARENT_TITLE = "Release Notes"
PARENT_BODY = "<p>One page per service, one entry per day it deployed (RC1-497).</p>"


@dataclass(frozen=True)
class DeployPath:
    repo: str
    service: str
    workflow: str  # the deploy workflow's file name


#: The deploy paths the digest covers. The pilot only; rolling out to another
#: repo is one row here plus the `release-notify.yml` call in its workflow.
PATHS = (DeployPath("tpm-automation-platform", "tpm-drift-detector", "fly-deploy.yml"),)


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    url: str
    story: str | None  # "RC1-497", from the branch name or the title


@dataclass(frozen=True)
class Run:
    id: int
    sha: str
    finished_at: datetime


@dataclass(frozen=True)
class DayEntry:
    path: DeployPath
    day: date
    deploys: int
    head_sha: str
    prs: tuple[PullRequest, ...]


# --------------------------------------------------------------------------- #
# GitHub: which pull requests a deploy carried
# --------------------------------------------------------------------------- #
def github_client(token: str | None) -> httpx.Client:
    """Every repo in `PATHS` is public, so the token only buys rate limit."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=GITHUB_API, headers=headers, timeout=30)


_STORY = re.compile(r"\brc1-(\d+)", re.IGNORECASE)


def story_key(branch: str, title: str) -> str | None:
    """The RC1 key a PR belongs to: the branch says it by convention, the
    title is the fallback for a branch that broke the convention."""
    for text in (branch, title):
        m = _STORY.search(text)
        if m:
            return f"RC1-{m.group(1)}"
    return None


def successful_runs(http: httpx.Client, repo: str, workflow: str) -> list[Run]:
    """Successful runs of one deploy workflow on the default branch, newest
    first. A failed or in-progress run is never in this list, which is what
    keeps an unshipped merge out of the notes."""
    resp = http.get(
        f"/repos/{OWNER}/{repo}/actions/workflows/{workflow}/runs",
        params={"branch": DEFAULT_BRANCH, "status": "success", "per_page": 50},
    )
    resp.raise_for_status()
    return [
        Run(
            id=r["id"],
            sha=r["head_sha"],
            finished_at=datetime.fromisoformat(r["updated_at"].replace("Z", "+00:00")),
        )
        for r in resp.json()["workflow_runs"]
    ]


def earlier_attempt_succeeded(http: httpx.Client, repo: str, run_id: int, attempt: int) -> bool:
    """True when this run already went green once. A re-run of a green run
    deploys the same sha again; announcing it twice would be noise."""
    for n in range(1, attempt):
        resp = http.get(f"/repos/{OWNER}/{repo}/actions/runs/{run_id}/attempts/{n}")
        resp.raise_for_status()
        if resp.json().get("conclusion") == "success":
            return True
    return False


def shipped_prs(http: httpx.Client, repo: str, base: str | None, head: str) -> list[PullRequest]:
    """The merged PRs whose commits lie in `base...head`, oldest merge first.

    Commits are mapped to PRs rather than parsed for "Merge pull request #N":
    the mapping holds for squash and rebase merges too. With no `base` (the
    first deploy the workflow ever recorded) only `head` itself is looked up.
    """
    if base is None:
        shas = [head]
    elif base == head:
        return []
    else:
        resp = http.get(f"/repos/{OWNER}/{repo}/compare/{base}...{head}", params={"per_page": 250})
        resp.raise_for_status()
        shas = [c["sha"] for c in resp.json()["commits"]]

    found: dict[int, tuple[str, PullRequest]] = {}
    for sha in shas:
        resp = http.get(f"/repos/{OWNER}/{repo}/commits/{sha}/pulls")
        resp.raise_for_status()
        for pr in resp.json():
            # A commit can also sit in an open or abandoned PR; only one that
            # merged into the default branch shipped.
            if not pr.get("merged_at") or pr["base"]["ref"] != DEFAULT_BRANCH:
                continue
            found[pr["number"]] = (
                pr["merged_at"],
                PullRequest(
                    number=pr["number"],
                    title=pr["title"],
                    url=pr["html_url"],
                    story=story_key(pr["head"]["ref"], pr["title"]),
                ),
            )
    return [pr for _, pr in sorted(found.values(), key=lambda pair: pair[0])]


# --------------------------------------------------------------------------- #
# Slack
# --------------------------------------------------------------------------- #
def _slack_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _story_url(story: str) -> str:
    return f"{settings.jira_base_url}/browse/{story}"


def render_deploy_message(path: DeployPath, sha: str, prs: list[PullRequest]) -> str:
    commit_url = f"https://github.com/{OWNER}/{path.repo}/commit/{sha}"
    lines = [f"*{path.service}* deployed <{commit_url}|{sha[:8]}>"]
    for pr in prs:
        line = f"• <{pr.url}|#{pr.number}> {_slack_text(pr.title)}"
        if pr.story:
            line += f" (<{_story_url(pr.story)}|{pr.story}>)"
        lines.append(line)
    return "\n".join(lines)


def post_slack(webhook_url: str, text: str) -> None:
    resp = httpx.post(webhook_url, json={"text": text}, timeout=15)
    resp.raise_for_status()


def notify(
    http: httpx.Client, path: DeployPath, *, sha: str, run_id: int, run_attempt: int
) -> str | None:
    """The #releases message for the deploy that just succeeded, or None when
    there is nothing to announce: a re-run of a run that was already green, or
    a deploy that carried no merged PR (a redeploy of the same sha)."""
    if earlier_attempt_succeeded(http, path.repo, run_id, run_attempt):
        return None
    # This run is still in progress, so it is not in the list; the filter
    # covers a re-run, whose id already has a completed attempt behind it.
    previous = [r for r in successful_runs(http, path.repo, path.workflow) if r.id != run_id]
    base = previous[0].sha if previous else None
    prs = shipped_prs(http, path.repo, base, sha)
    if not prs:
        return None
    return render_deploy_message(path, sha, prs)


# --------------------------------------------------------------------------- #
# The daily digest
# --------------------------------------------------------------------------- #
def day_window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=LOCAL_TZ)
    return start, datetime.combine(day + timedelta(days=1), time.min, tzinfo=LOCAL_TZ)


def collect_day(http: httpx.Client, path: DeployPath, day: date) -> DayEntry | None:
    """What one service shipped on one local day, or None when it shipped
    nothing. The range runs from the last deploy before the day to the last
    deploy of the day, so the day's entry is the day's net change."""
    start, end = day_window(day)
    runs = successful_runs(http, path.repo, path.workflow)
    today = [r for r in runs if start <= r.finished_at < end]
    if not today:
        return None
    before = [r for r in runs if r.finished_at < start]
    head = today[0].sha
    prs = shipped_prs(http, path.repo, before[0].sha if before else None, head)
    if not prs:
        return None
    return DayEntry(path=path, day=day, deploys=len(today), head_sha=head, prs=tuple(prs))


def render_section(entry: DayEntry) -> str:
    """One day as Confluence storage XHTML. The `<h2>` holds the ISO date and
    nothing else: it is the key `merge_day` finds the section by."""
    commit_url = f"https://github.com/{OWNER}/{entry.path.repo}/commit/{entry.head_sha}"
    deploys = "1 deploy" if entry.deploys == 1 else f"{entry.deploys} deploys"
    items = []
    for pr in entry.prs:
        item = f'<a href="{html.escape(pr.url)}">#{pr.number}</a> {html.escape(pr.title)}'
        if pr.story:
            item += f' (<a href="{html.escape(_story_url(pr.story))}">{pr.story}</a>)'
        items.append(f"<li>{item}</li>")
    return (
        f"<h2>{entry.day.isoformat()}</h2>"
        f'<p>{deploys}, ending at <a href="{html.escape(commit_url)}">{entry.head_sha[:8]}</a>.</p>'
        f"<ul>{''.join(items)}</ul>"
    )


# Confluence can hand the heading back with attributes it added.
_SECTION_START = re.compile(r"(?=<h2[^>]*>\s*\d{4}-\d{2}-\d{2}\s*</h2>)")
_SECTION_DAY = re.compile(r"<h2[^>]*>\s*(\d{4}-\d{2}-\d{2})\s*</h2>")


def merge_day(body: str, day: date, section: str) -> str:
    """`body` with `day`'s section replaced, or inserted, newest day first.
    Whatever precedes the first dated heading is kept as it is."""
    preamble, *sections = _SECTION_START.split(body)
    by_day = {_SECTION_DAY.match(s).group(1): s for s in sections}
    by_day[day.isoformat()] = section
    return preamble + "".join(by_day[d] for d in sorted(by_day, reverse=True))


class Confluence:
    """The four calls the digest needs, on the v2 REST API."""

    def __init__(self, http: httpx.Client, base_url: str):
        self._http = http
        self._wiki = f"{base_url}/wiki"

    def _api(self, method: str, path: str, **kwargs) -> dict:
        resp = self._http.request(method, f"{self._wiki}/api/v2{path}", **kwargs)
        resp.raise_for_status()
        return resp.json()

    def space_id(self, key: str) -> str:
        results = self._api("GET", "/spaces", params={"keys": key})["results"]
        if not results:
            raise LookupError(f"Confluence space {key} not found")
        return results[0]["id"]

    def find_page(self, space_id: str, title: str) -> dict | None:
        results = self._api(
            "GET",
            "/pages",
            params={"space-id": space_id, "title": title, "body-format": "storage"},
        )["results"]
        return results[0] if results else None

    def create_page(self, space_id: str, title: str, body: str, parent_id: str | None) -> dict:
        payload = {
            "spaceId": space_id,
            "status": "current",
            "title": title,
            "body": {"representation": "storage", "value": body},
        }
        if parent_id:
            payload["parentId"] = parent_id
        return self._api("POST", "/pages", json=payload)

    def update_page(self, page: dict, body: str) -> dict:
        return self._api(
            "PUT",
            f"/pages/{page['id']}",
            json={
                "id": page["id"],
                "status": "current",
                "title": page["title"],
                "body": {"representation": "storage", "value": body},
                "version": {"number": page["version"]["number"] + 1},
            },
        )

    def url(self, page: dict) -> str:
        return f"{self._wiki}{page['_links']['webui']}"


def confluence_client(email: str, token: str) -> httpx.Client:
    return httpx.Client(auth=(email, token), timeout=30)


def page_title(service: str) -> str:
    # Titles are unique per space, so the service page cannot just be the
    # service name — that would collide with any page about the service.
    return f"Release notes: {service}"


def publish(wiki: Confluence, entry: DayEntry) -> str | None:
    """Write one day's entry to its service page. Returns the page URL when
    the page changed, None when it already said exactly this — so a re-run
    neither bumps the page version nor repeats the Slack message."""
    space = wiki.space_id(SPACE_KEY)
    parent = wiki.find_page(space, PARENT_TITLE) or wiki.create_page(
        space, PARENT_TITLE, PARENT_BODY, None
    )
    title = page_title(entry.path.service)
    section = render_section(entry)
    page = wiki.find_page(space, title)
    if page is None:
        return wiki.url(wiki.create_page(space, title, section, parent["id"]))
    current = page["body"]["storage"]["value"]
    merged = merge_day(current, entry.day, section)
    if merged == current:
        return None
    wiki.update_page(page, merged)
    return wiki.url(page)


def render_digest_message(day: date, published: list[tuple[DayEntry, str]]) -> str:
    lines = [f"Release notes for {day.isoformat()}"]
    for entry, url in published:
        n = len(entry.prs)
        lines.append(f"• <{url}|{entry.path.service}>: {n} PR{'s' if n != 1 else ''}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _describe(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code} on {e.request.url.path}"
    return f"{type(e).__name__}: {e}"


def _notify_main(args: argparse.Namespace) -> int:
    path = DeployPath(args.repo, args.service, args.workflow)
    with github_client(settings.github_token) as http:
        text = notify(http, path, sha=args.sha, run_id=args.run_id, run_attempt=args.run_attempt)
    if text is None:
        print("nothing to announce: a re-run of a green run, or no merged PR in this deploy")
        return 0
    print(text)
    if args.dry_run:
        print("dry run — not posted")
        return 0
    if not settings.slack_releases_webhook_url:
        print("SLACK_RELEASES_WEBHOOK_URL is not set — nothing posted", file=sys.stderr)
        return 2
    post_slack(settings.slack_releases_webhook_url, text)
    print("posted to #releases")
    return 0


def _digest_main(args: argparse.Namespace) -> int:
    day = args.date or (datetime.now(LOCAL_TZ).date() - timedelta(days=1))
    entries: list[DayEntry] = []
    failed: dict[str, str] = {}
    with github_client(settings.github_token) as http:
        for path in PATHS:
            try:
                entry = collect_day(http, path, day)
            except httpx.HTTPError as e:
                failed[path.service] = _describe(e)
                continue
            if entry is None:
                print(f"{path.service}: no deploys with merged PRs on {day}")
            else:
                entries.append(entry)

    if args.dry_run:
        for entry in entries:
            print(f"{entry.path.service}:\n{render_section(entry)}")
        print(f"dry run — {len(entries)} entries not written")
    elif not (settings.jira_email and settings.jira_api_token):
        print("JIRA_EMAIL / JIRA_API_TOKEN are not set — nothing written", file=sys.stderr)
        return 2
    else:
        published: list[tuple[DayEntry, str]] = []
        with confluence_client(settings.jira_email, settings.jira_api_token) as http:
            wiki = Confluence(http, settings.jira_base_url)
            for entry in entries:
                try:
                    url = publish(wiki, entry)
                except (httpx.HTTPError, LookupError) as e:
                    failed[entry.path.service] = _describe(e)
                    continue
                if url is None:
                    print(f"{entry.path.service}: entry for {day} already current")
                else:
                    print(f"{entry.path.service}: wrote {day} ({len(entry.prs)} PRs) to {url}")
                    published.append((entry, url))
        if published and settings.slack_releases_webhook_url:
            try:
                post_slack(
                    settings.slack_releases_webhook_url, render_digest_message(day, published)
                )
            except httpx.HTTPError as e:
                failed["slack"] = _describe(e)
        elif published:
            print("SLACK_RELEASES_WEBHOOK_URL is not set — pages written, no message posted")

    for name, why in failed.items():
        print(f"::error title=Release notes: {name}::{why}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m kpi.release_notes",
        description="Release notes from merged PRs: a Slack message per deploy and a "
        "daily Confluence digest (RC1-497).",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    n = sub.add_parser("notify", help="announce the deploy that just succeeded")
    n.add_argument("--repo", required=True)
    n.add_argument("--service", required=True)
    n.add_argument("--workflow", required=True, help="the deploy workflow's file name")
    n.add_argument("--sha", required=True)
    n.add_argument("--run-id", required=True, type=int)
    n.add_argument("--run-attempt", type=int, default=1)
    n.add_argument("--dry-run", action="store_true", help="print the message; do not post")
    n.set_defaults(run=_notify_main)

    d = sub.add_parser("digest", help="write one day's entries to Confluence")
    d.add_argument(
        "--date",
        type=date.fromisoformat,
        help="the local day to write, YYYY-MM-DD (default: yesterday, America/New_York)",
    )
    d.add_argument("--dry-run", action="store_true", help="print the entries; do not write")
    d.set_defaults(run=_digest_main)

    args = ap.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
