"""Jira data-acquisition layer (RC1-134 [3/9]).

The shared collector: fetches issues, dependency links, and scheduling-field
changelog for a project and normalizes everything into typed models. The
parse_* functions are pure (raw dict -> models) so they can be unit-tested
against canned fixtures with no live API calls; JiraCollector wraps them with a
retrying httpx client.

Carries over lessons from the n8n build: explicit timeouts, retry-with-backoff
on transient failures, and no raw dicts escaping past collect().
"""

from __future__ import annotations

import time
from datetime import date, datetime

import httpx

from collectors.models import DateChange, DependencyLink, Issue, ProjectSnapshot

# RC1 instance specifics (see the rc1-jira-preflight notes). Instance-scoped —
# lift into config when a second Jira site appears.
START_DATE_FIELD = "customfield_10015"
BLOCKS_LINK = "Blocks"

ISSUE_FIELDS = [
    "summary",
    "status",
    "priority",
    "assignee",
    "duedate",
    START_DATE_FIELD,
    "issuelinks",
]

# changelog field ids / names we care about -> normalized label
_DATE_FIELD_IDS = {"duedate": "duedate", START_DATE_FIELD: "start_date"}
_DATE_FIELD_NAMES = {"duedate": "duedate", "Due date": "duedate", "Start date": "start_date"}

_RETRY_STATUS = {429, 500, 502, 503, 504}


class JiraError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Pure parsers (raw Jira JSON -> models). No I/O — unit-tested against fixtures.
# --------------------------------------------------------------------------- #
def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def parse_issue(raw: dict) -> Issue:
    f = raw.get("fields", {})
    status = f.get("status") or {}
    category = (status.get("statusCategory") or {}).get("name") or ""
    priority = (f.get("priority") or {}).get("name")
    assignee = f.get("assignee") or {}
    return Issue(
        key=raw["key"],
        summary=f.get("summary") or "",
        status=status.get("name") or "",
        status_category=category,
        priority=priority,
        assignee_id=assignee.get("accountId"),
        assignee_name=assignee.get("displayName"),
        due=_parse_date(f.get("duedate")),
        start=_parse_date(f.get(START_DATE_FIELD)),
    )


def parse_links(raw: dict) -> list[DependencyLink]:
    """Extract directed Blocks edges from one issue's ``issuelinks``.

    Direction gotcha (verified against RC1): on issue X, a link carrying
    ``outwardIssue`` means "X blocks that issue"; ``inwardIssue`` means "X is
    blocked by that issue". Both ends of a link report it, so callers dedupe.
    """
    key = raw["key"]
    out: list[DependencyLink] = []
    for link in raw.get("fields", {}).get("issuelinks") or []:
        if (link.get("type") or {}).get("name") != BLOCKS_LINK:
            continue
        if link.get("outwardIssue"):
            out.append(DependencyLink(upstream=key, downstream=link["outwardIssue"]["key"]))
        elif link.get("inwardIssue"):
            out.append(DependencyLink(upstream=link["inwardIssue"]["key"], downstream=key))
    return out


def parse_changelog(histories: list[dict]) -> list[DateChange]:
    """Pull duedate / start-date changes from raw changelog histories, oldest first."""
    changes: list[DateChange] = []
    for h in histories:
        changed_at = _parse_dt(h["created"])
        for item in h.get("items", []):
            label = _DATE_FIELD_IDS.get(item.get("fieldId") or "") or _DATE_FIELD_NAMES.get(
                item.get("field") or ""
            )
            if not label:
                continue
            changes.append(
                DateChange(
                    field=label,
                    from_date=_parse_date(item.get("from") or item.get("fromString")),
                    to_date=_parse_date(item.get("to") or item.get("toString")),
                    changed_at=changed_at,
                )
            )
    changes.sort(key=lambda c: c.changed_at)
    return changes


def dedupe_links(links: list[DependencyLink]) -> list[DependencyLink]:
    seen: set[tuple[str, str, str]] = set()
    out: list[DependencyLink] = []
    for link in links:
        sig = (link.upstream, link.downstream, link.link_type)
        if sig not in seen:
            seen.add(sig)
            out.append(link)
    return out


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #
class JiraCollector:
    def __init__(
        self,
        base_url: str,
        email: str = "",
        token: str = "",
        *,
        timeout: float = 30.0,
        max_retries: int = 4,
        backoff_base: float = 0.5,
        client: httpx.Client | None = None,
    ):
        self._api = f"{base_url.rstrip('/')}/rest/api/3"
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        if client is not None:  # injected (tests use httpx.MockTransport)
            self._c = client
        else:
            if not (email and token):
                raise JiraError("JIRA_EMAIL and JIRA_API_TOKEN must be set (see .env.example).")
            self._c = httpx.Client(
                auth=(email, token),
                timeout=httpx.Timeout(timeout, connect=10.0),
            )

    def __enter__(self) -> JiraCollector:
        return self

    def __exit__(self, *exc: object) -> None:
        self._c.close()

    def _request(self, method: str, path: str, **kw: object) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                r = self._c.request(method, f"{self._api}{path}", **kw)
            except httpx.TransportError as e:  # timeouts, connection resets
                last_exc = e
                if attempt < self._max_retries:
                    time.sleep(self._backoff_base * 2**attempt)
                    continue
                raise JiraError(f"{method} {path} failed after retries: {e}") from e

            if r.status_code in _RETRY_STATUS and attempt < self._max_retries:
                time.sleep(self._retry_after(r) or self._backoff_base * 2**attempt)
                continue
            if r.status_code >= 300:
                raise JiraError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:400]}")
            return r
        raise JiraError(f"{method} {path} exhausted retries: {last_exc}")

    @staticmethod
    def _retry_after(r: httpx.Response) -> float | None:
        raw = r.headers.get("Retry-After")
        if raw and raw.isdigit():
            return float(raw)
        return None

    def _search(self, jql: str) -> list[dict]:
        issues: list[dict] = []
        token: str | None = None
        while True:
            body: dict[str, object] = {"jql": jql, "fields": ISSUE_FIELDS, "maxResults": 100}
            if token:
                body["nextPageToken"] = token
            data = self._request("POST", "/search/jql", json=body).json()
            issues.extend(data.get("issues", []))
            token = data.get("nextPageToken")
            if not token:
                break
        return issues

    def _changelog(self, key: str) -> list[dict]:
        histories: list[dict] = []
        start = 0
        while True:
            params = {"startAt": start, "maxResults": 100}
            data = self._request("GET", f"/issue/{key}/changelog", params=params).json()
            histories.extend(data.get("values", []))
            if data.get("isLast", True) or not data.get("values"):
                break
            start += len(data["values"])
        return histories

    def date_changes(self, key: str) -> list[DateChange]:
        return parse_changelog(self._changelog(key))

    def collect(self, project_key: str, *, jql: str | None = None) -> ProjectSnapshot:
        """Fetch and normalize a whole project into a ProjectSnapshot."""
        jql = jql or f"project = {project_key} ORDER BY key ASC"
        raw_issues = self._search(jql)

        issues: list[Issue] = []
        links: list[DependencyLink] = []
        for raw in raw_issues:
            issue = parse_issue(raw)
            issue.date_changes = self.date_changes(issue.key)
            issues.append(issue)
            links.extend(parse_links(raw))

        return ProjectSnapshot(
            project_key=project_key,
            issues=issues,
            links=dedupe_links(links),
        )
