"""Release notes: each deploy writes its service's Confluence page, then tells
#releases (RC1-497, RC1-502).

A release here is a production deploy that succeeded — most of the estate
ships on push to `main`, so there is no tag to hang a note on. The note is
built from merged pull requests, not a diff: the commits between the
previously deployed sha and this one are mapped back to the PRs that carried
them, and each PR names its story through the `rc1-NNN-slug` branch.

Which PRs shipped is decided here, in Python. A model only writes the short
plain-language summary that opens a day's entry, for a reader who cannot open
GitHub (RC1-502); the PR list under it is the audit trail. No summary is
never a reason to skip the entry: without a model key, or when the call
fails, the entry is the PR list alone.

Two commands:

    notify   run by a deploy workflow after the deploy succeeded (through the
             reusable `release-notify.yml`). Rewrites today's section on the
             service's page so it covers everything shipped today, then posts
             one #releases message that links the page. The page is written
             first, so the link never leads to a page that predates the
             deploy.
    digest   run once a day, for yesterday: the sweep. Writes any entry a
             deploy-time write missed and leaves a page that is already right
             untouched, without a model call. One #releases message links
             the pages it had to change.

Only successful deploy runs are read, so a merge whose deploy failed is not
reported as shipped: it appears on the day a later deploy carries it out.

Both commands write to Confluence. That is publishing a report, the same
class as the drift digest going to Slack — it is not the infrastructure-state
sync the repo forbids scheduled jobs from doing.

Run: `python -m kpi.release_notes notify --repo R --service S --workflow W
     --sha SHA --run-id ID [--run-attempt N] [--dry-run]`
     `python -m kpi.release_notes digest [--date YYYY-MM-DD] [--dry-run]`
Env (through `config.settings`): `GITHUB_TOKEN`, `SLACK_RELEASES_WEBHOOK_URL`,
`JIRA_EMAIL` + `JIRA_API_TOKEN` (one Atlassian token serves Jira and
Confluence) and `ANTHROPIC_API_KEY`. Every one is optional; each missing one
removes its piece and says so.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from config import settings
from observability import enable_llm_obs

try:  # documented optional-dep exception: without the SDK the notes have no summary
    import anthropic
except ImportError:  # pragma: no cover - exercised only without anthropic
    anthropic = None

OWNER = "snacksnack"
GITHUB_API = "https://api.github.com"
DEFAULT_BRANCH = "main"

#: A "day" of releases is a day where Reid works, not a UTC day: an evening
#: session would otherwise split across two entries.
LOCAL_TZ = ZoneInfo("America/New_York")

SPACE_KEY = "RC1"
PARENT_TITLE = "Release Notes"
PARENT_BODY = "<p>One page per service, one entry per day it deployed (RC1-497).</p>"

#: The summary is a short rewrite of a handful of PR descriptions, the small
#: model's seat (RC1-502).
SUMMARY_MODEL = "claude-haiku-4-5"
ML_APP = "release-notes"
_TEMPLATE = Path(__file__).parent / "templates" / "release_summary.md"
#: A PR description past this length is cut, and the payload says so. A
#: Dependabot body is mostly an upstream changelog; its first screen says
#: what was bumped.
BODY_CHARS = 6000


@dataclass(frozen=True)
class DeployPath:
    repo: str
    service: str
    workflow: str  # the deploy workflow's file name
    #: True for a library that releases on a pushed `v*` tag instead of
    #: deploying from the default branch. Its runs are identified by tag name.
    tags: bool = False


#: The deploy paths the digest covers. Adding a repo is one row here plus the
#: `release-notify.yml` call in its deploy workflow. The service is the name
#: the deploy reports to DORA; the summarizer's one workflow ships two DORA
#: services and gets one page, under the backend's name.
PATHS = (
    DeployPath("tpm-automation-platform", "tpm-drift-detector", "fly-deploy.yml"),
    DeployPath("pr_agent", "pr-review-agent-snacksnack", "fly-deploy.yml"),
    DeployPath("launch-planner-agent", "launch-planner-agent", "fly-deploy.yml"),
    DeployPath("reid_basic", "hihelloreid", "heroku-release.yml"),
    DeployPath("ai-incident-summarizer", "incident-summarizer", "deploy.yml"),
    DeployPath("stale-ticket-bot", "stale-ticket-bot", "deploy.yml"),
    DeployPath("agent-evals", "agent-evals", "release.yml", tags=True),
)


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    url: str
    story: str | None  # "RC1-497", from the branch name or the title
    body: str = ""  # the author's description; only the summary reads it


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


@dataclass(frozen=True)
class Summary:
    text: str
    points: tuple[str, ...] = ()


@dataclass(frozen=True)
class Published:
    url: str
    changed: bool  # False: the page already said exactly this
    summary: str  # "new" (a model wrote it), "kept" (the page's own) or "none"


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


#: How many times the run list is fetched; the answer with the most runs wins.
RUN_LIST_READS = 3


def successful_runs(http: httpx.Client, path: DeployPath) -> list[Run]:
    """Successful runs of one deploy workflow, newest first. A failed or
    in-progress run is never in this list, which is what keeps an unshipped
    merge out of the notes.

    The list is read `RUN_LIST_READS` times and the longest answer kept.
    GitHub intermittently serves this endpoint from a snapshot weeks old: a
    200 with a complete-looking list that simply ends early (caught
    2026-10-05: 84 runs ending 09-17 for a workflow that ran the day before,
    1 call in 72). One stale read is indistinguishable from a quiet day, and
    the first scheduled digest lost two services' entries to it. A run list
    only grows, so the longest of several reads is the newest.

    The success filter is applied here rather than with `status=success` so
    all reads are of the same unfiltered list and their lengths compare.

    A tag-released path is not filtered by branch (a tag push has none), and
    its runs carry the tag name where the others carry a sha: GitHub resolves
    a tag anywhere it takes a commit, and for an annotated tag the run's own
    sha can be the tag object, which the compare endpoint cannot walk.
    """
    params: dict[str, str | int] = {"per_page": 100}
    if not path.tags:
        params["branch"] = DEFAULT_BRANCH
    best: dict = {"total_count": -1, "workflow_runs": []}
    for _ in range(RUN_LIST_READS):
        resp = http.get(
            f"/repos/{OWNER}/{path.repo}/actions/workflows/{path.workflow}/runs", params=params
        )
        resp.raise_for_status()
        if resp.json()["total_count"] > best["total_count"]:
            best = resp.json()
    return [
        Run(
            id=r["id"],
            sha=r["head_branch"] if path.tags else r["head_sha"],
            finished_at=datetime.fromisoformat(r["updated_at"].replace("Z", "+00:00")),
        )
        for r in best["workflow_runs"]
        if r["conclusion"] == "success"
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
                    body=pr.get("body") or "",
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


def shipped_ref(path: DeployPath, ref: str) -> tuple[str, str]:
    """(url, label) for what a deploy shipped: the commit, or the tag."""
    repo_url = f"https://github.com/{OWNER}/{path.repo}"
    if path.tags:
        return f"{repo_url}/releases/tag/{ref}", ref
    return f"{repo_url}/commit/{ref}", ref[:8]


def render_deploy_message(
    path: DeployPath, sha: str, prs: list[PullRequest], notes_url: str | None = None
) -> str:
    url, label = shipped_ref(path, sha)
    verb = "released" if path.tags else "deployed"
    lines = [f"*{path.service}* {verb} <{url}|{label}>"]
    if notes_url:
        lines[0] += f" · <{notes_url}|release notes>"
    for pr in prs:
        line = f"• <{pr.url}|#{pr.number}> {_slack_text(pr.title)}"
        if pr.story:
            line += f" (<{_story_url(pr.story)}|{pr.story}>)"
        lines.append(line)
    return "\n".join(lines)


def post_slack(webhook_url: str, text: str) -> None:
    resp = httpx.post(webhook_url, json={"text": text}, timeout=15)
    resp.raise_for_status()


def deploy_prs(
    http: httpx.Client, path: DeployPath, *, sha: str, run_id: int, run_attempt: int
) -> list[PullRequest]:
    """The PRs the deploy that just succeeded carried. Empty when there is
    nothing to announce: a re-run of a run that was already green, or a
    deploy that carried no merged PR (a redeploy of the same sha)."""
    if earlier_attempt_succeeded(http, path.repo, run_id, run_attempt):
        return []
    # This run is still in progress, so it is not in the list; the filter
    # covers a re-run, whose id already has a completed attempt behind it.
    previous = [r for r in successful_runs(http, path) if r.id != run_id]
    base = previous[0].sha if previous else None
    return shipped_prs(http, path.repo, base, sha)


# --------------------------------------------------------------------------- #
# A day's entry
# --------------------------------------------------------------------------- #
def day_window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=LOCAL_TZ)
    return start, datetime.combine(day + timedelta(days=1), time.min, tzinfo=LOCAL_TZ)


def collect_day(
    http: httpx.Client, path: DeployPath, day: date, *, current: Run | None = None
) -> DayEntry | None:
    """What one service shipped on one local day, or None when it shipped
    nothing. The range runs from the last deploy before the day to the last
    deploy of the day, so the day's entry is the day's net change.

    `current` is the deploy calling from inside its own run: that run is
    still in progress, so the run list does not have it yet, and it is the
    newest deploy there is."""
    start, end = day_window(day)
    runs = successful_runs(http, path)
    if current is not None:
        runs = [current, *(r for r in runs if r.id != current.id)]
    today = [r for r in runs if start <= r.finished_at < end]
    if not today:
        return None
    before = [r for r in runs if r.finished_at < start]
    head = today[0].sha
    prs = shipped_prs(http, path.repo, before[0].sha if before else None, head)
    if not prs:
        return None
    return DayEntry(path=path, day=day, deploys=len(today), head_sha=head, prs=tuple(prs))


def summary_payload(entry: DayEntry) -> dict:
    """The exact facts handed to the model: the day's PRs as their authors
    described them."""
    return {
        "service": entry.path.service,
        "day": entry.day.isoformat(),
        "pull_requests": [
            {
                "number": pr.number,
                "title": pr.title,
                "story": pr.story,
                "description": pr.body[:BODY_CHARS],
                "description_truncated": len(pr.body) > BODY_CHARS,
            }
            for pr in entry.prs
        ],
    }


_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "points": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "points"],
    "additionalProperties": False,
}


def model_client():
    """The Anthropic client, or None when there is no SDK or no key."""
    if anthropic is None or not settings.anthropic_api_key:
        return None
    return anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=60.0, max_retries=2)


def summarize(client, entry: DayEntry) -> Summary | None:
    """A plain-language summary of one day's entry, or None when the model
    could not give one. Never raises: the summary decorates the record, and
    a deploy's notes must not depend on the model being up."""
    try:
        resp = client.messages.create(
            model=SUMMARY_MODEL,
            max_tokens=1024,
            system=_TEMPLATE.read_text(),
            messages=[{"role": "user", "content": json.dumps(summary_payload(entry))}],
            output_config={"format": {"type": "json_schema", "schema": _SUMMARY_SCHEMA}},
        )
        if resp.stop_reason != "end_turn":
            raise ValueError(f"stop_reason {resp.stop_reason}")
        data = json.loads(next(b.text for b in resp.content if b.type == "text"))
        text = data["summary"].strip()
        if not text:
            raise ValueError("empty summary")
        return Summary(text, tuple(p.strip() for p in data["points"] if p.strip()))
    except Exception as e:  # any failure is "no summary", never a failed run
        print(
            f"::warning title=Release notes: no summary for {entry.path.service}::"
            f"{type(e).__name__}: {e}. The entry is the PR list alone."
        )
        return None


def summarizer() -> Callable[[DayEntry], Summary | None]:
    """What `publish` calls when an entry needs a summary written. It warns
    only then: a sweep that finds every page current never needs the key."""
    client = model_client()
    if client is not None:
        enable_llm_obs(ML_APP, service="kpi.release_notes")

    def run(entry: DayEntry) -> Summary | None:
        if client is None:
            print(
                f"::warning title=Release notes: no summary for {entry.path.service}::"
                "ANTHROPIC_API_KEY is not set. The entry is the PR list alone."
            )
            return None
        return summarize(client, entry)

    return run


def _text(value: str) -> str:
    # Quotes stay as they are: Confluence hands `&quot;` back as `"`, and a
    # section that does not round-trip is rewritten on every run.
    return html.escape(value, quote=False)


#: Closes the summary. It tells the reader who wrote the lines above it, and
#: it is how a later run finds the summary a page already has.
SUMMARY_NOTE = "<p><em>Summary written by AI from the pull requests below.</em></p>"
_SUMMARY_NOTE = re.compile(r"<p[^>]*>\s*<em[^>]*>\s*Summary written by AI[^<]*</em>\s*</p>")
_PR_NUMBER = re.compile(r">#(\d+)</a>")


def render_summary(summary: Summary | None) -> str:
    if summary is None:
        return ""
    points = "".join(f"<li>{_text(p)}</li>" for p in summary.points)
    return f"<p>{_text(summary.text)}</p>" + (f"<ul>{points}</ul>" if points else "") + SUMMARY_NOTE


def render_facts(entry: DayEntry) -> str:
    """The part of a day's section that Python decides: how many deploys,
    where they ended, and the PRs they carried."""
    url, label = shipped_ref(entry.path, entry.head_sha)
    noun = "release" if entry.path.tags else "deploy"
    deploys = f"1 {noun}" if entry.deploys == 1 else f"{entry.deploys} {noun}s"
    items = []
    for pr in entry.prs:
        item = f'<a href="{html.escape(pr.url)}">#{pr.number}</a> {_text(pr.title)}'
        if pr.story:
            item += f' (<a href="{html.escape(_story_url(pr.story))}">{pr.story}</a>)'
        items.append(f"<li>{item}</li>")
    return (
        f'<p>{deploys}, ending at <a href="{html.escape(url)}">{label}</a>.</p>'
        f"<ul>{''.join(items)}</ul>"
    )


def render_section(entry: DayEntry, summary_html: str = "") -> str:
    """One day as Confluence storage XHTML: the date, the summary when there
    is one, then the facts. The `<h2>` holds the ISO date and nothing else:
    it is the key `merge_day` finds the section by."""
    return f"<h2>{entry.day.isoformat()}</h2>{summary_html}{render_facts(entry)}"


# Confluence can hand the heading back with attributes it added.
_SECTION_START = re.compile(r"(?=<h2[^>]*>\s*\d{4}-\d{2}-\d{2}\s*</h2>)")
_SECTION_DAY = re.compile(r"<h2[^>]*>\s*(\d{4}-\d{2}-\d{2})\s*</h2>")


def kept_summary(body: str, entry: DayEntry) -> str:
    """The summary the page already has for this day, as it stands, when it
    still describes the same PRs; otherwise "". A summary is a reading of a
    set of PRs, so the same set does not buy a second model call, and a
    changed set does not keep the old words."""
    for section in _SECTION_START.split(body)[1:]:
        heading = _SECTION_DAY.match(section)
        if heading.group(1) != entry.day.isoformat():
            continue
        note = _SUMMARY_NOTE.search(section)
        if note is None:
            return ""
        listed = [int(n) for n in _PR_NUMBER.findall(section[note.end() :])]
        if listed != [pr.number for pr in entry.prs]:
            return ""
        return section[heading.end() : note.end()]
    return ""


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


def publish(
    wiki: Confluence,
    entry: DayEntry,
    summarize: Callable[[DayEntry], Summary | None] | None = None,
) -> Published:
    """Write one day's entry to its service page.

    `summarize` is called only when the entry needs a summary the page does
    not already have for these PRs. `changed` is False when the page already
    said exactly this — so a re-run neither bumps the page version nor
    repeats a Slack message."""
    space = wiki.space_id(SPACE_KEY)
    parent = wiki.find_page(space, PARENT_TITLE) or wiki.create_page(
        space, PARENT_TITLE, PARENT_BODY, None
    )
    title = page_title(entry.path.service)
    page = wiki.find_page(space, title)
    current = page["body"]["storage"]["value"] if page else ""

    summary_html, source = kept_summary(current, entry), "kept"
    if not summary_html:
        summary_html = render_summary(summarize(entry) if summarize else None)
        source = "new" if summary_html else "none"
    section = render_section(entry, summary_html)

    if page is None:
        created = wiki.create_page(space, title, section, parent["id"])
        return Published(wiki.url(created), True, source)
    merged = merge_day(current, entry.day, section)
    if merged != current:
        wiki.update_page(page, merged)
    return Published(wiki.url(page), merged != current, source)


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


def _warn(title: str, why: str) -> None:
    print(f"::warning title=Release notes: {title}::{why}")


def _record(entry: DayEntry) -> str | None:
    """Write the day's entry at deploy time; the page URL, or None when the
    page could not be written. Never raises: the deploy is live, its #releases
    message still goes out, and the daily sweep writes what this missed."""
    if not (settings.jira_email and settings.jira_api_token):
        _warn(
            "page not written",
            "JIRA_EMAIL / JIRA_API_TOKEN are not set. The daily sweep will record this deploy.",
        )
        return None
    try:
        with confluence_client(settings.jira_email, settings.jira_api_token) as http:
            published = publish(Confluence(http, settings.jira_base_url), entry, summarizer())
    except (httpx.HTTPError, LookupError) as e:
        _warn("page not written", f"{_describe(e)}. The daily sweep will record this deploy.")
        return None
    state = "wrote" if published.changed else "already current:"
    print(f"{state} {entry.day} (summary: {published.summary}) {published.url}")
    return published.url


def _notify_main(args: argparse.Namespace) -> int:
    # A path the digest knows carries its own settings (a tag-released repo);
    # any other caller is an ordinary deploy from the default branch.
    path = next(
        (p for p in PATHS if (p.repo, p.workflow) == (args.repo, args.workflow)),
        DeployPath(args.repo, args.service, args.workflow),
    )
    entry: DayEntry | None = None
    with github_client(settings.github_token) as http:
        prs = deploy_prs(http, path, sha=args.sha, run_id=args.run_id, run_attempt=args.run_attempt)
        if not prs:
            print("nothing to announce: a re-run of a green run, or no merged PR in this deploy")
            return 0
        now = datetime.now(UTC)
        try:
            entry = collect_day(
                http, path, now.astimezone(LOCAL_TZ).date(), current=Run(args.run_id, args.sha, now)
            )
        except httpx.HTTPError as e:
            _warn("page not written", f"{_describe(e)}. The daily sweep will record this deploy.")

    if args.dry_run:
        if entry is not None:
            print(render_section(entry, render_summary(summarizer()(entry))))
        print(render_deploy_message(path, args.sha, prs))
        print("dry run — page not written, message not posted")
        return 0
    notes_url = _record(entry) if entry is not None else None
    text = render_deploy_message(path, args.sha, prs, notes_url)
    print(text)
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
        changed: list[tuple[DayEntry, str]] = []
        summarize_entry = summarizer()
        with confluence_client(settings.jira_email, settings.jira_api_token) as http:
            wiki = Confluence(http, settings.jira_base_url)
            for entry in entries:
                try:
                    published = publish(wiki, entry, summarize_entry)
                except (httpx.HTTPError, LookupError) as e:
                    failed[entry.path.service] = _describe(e)
                    continue
                if not published.changed:
                    print(f"{entry.path.service}: entry for {day} already current")
                else:
                    print(
                        f"{entry.path.service}: wrote {day} ({len(entry.prs)} PRs, "
                        f"summary: {published.summary}) to {published.url}"
                    )
                    changed.append((entry, published.url))
        if changed and settings.slack_releases_webhook_url:
            try:
                post_slack(settings.slack_releases_webhook_url, render_digest_message(day, changed))
            except httpx.HTTPError as e:
                failed["slack"] = _describe(e)
        elif changed:
            print("SLACK_RELEASES_WEBHOOK_URL is not set — pages written, no message posted")

    for name, why in failed.items():
        print(f"::error title=Release notes: {name}::{why}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m kpi.release_notes",
        description="Release notes from merged PRs: each deploy writes its Confluence "
        "page and posts to Slack; a daily sweep repairs misses (RC1-497, RC1-502).",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    n = sub.add_parser("notify", help="record and announce the deploy that just succeeded")
    n.add_argument("--repo", required=True)
    n.add_argument("--service", required=True)
    n.add_argument("--workflow", required=True, help="the deploy workflow's file name")
    n.add_argument("--sha", required=True, help="the deployed sha, or the tag for a release")
    n.add_argument("--run-id", required=True, type=int)
    n.add_argument("--run-attempt", type=int, default=1)
    n.add_argument(
        "--dry-run", action="store_true", help="print the entry and the message; write nothing"
    )
    n.set_defaults(run=_notify_main)

    d = sub.add_parser("digest", help="the daily sweep: write the entries a day is missing")
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
