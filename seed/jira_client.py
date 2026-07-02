"""Minimal Jira Cloud REST client for seeding — just what [2/9] needs.

Not the production collector (that's [3/9], collectors/jira.py). This is a
small, dependency-light wrapper used by the seed script and its teardown.
"""

from __future__ import annotations

import httpx

# Static IDs for the RC1 instance (verified during [2/9] pre-flight).
STORY_TYPE_ID = "10009"
EPIC_TYPE_ID = "10005"
START_DATE_FIELD = "customfield_10015"
BLOCKS_LINK = "Blocks"


class JiraError(RuntimeError):
    pass


class JiraClient:
    def __init__(self, base_url: str, email: str, token: str, *, timeout: float = 30.0):
        if not (email and token):
            raise JiraError("JIRA_EMAIL and JIRA_API_TOKEN must be set (see .env.example).")
        self._api = f"{base_url.rstrip('/')}/rest/api/3"
        self._c = httpx.Client(auth=(email, token), timeout=timeout)

    def __enter__(self) -> JiraClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self._c.close()

    def _req(self, method: str, path: str, **kw: object) -> httpx.Response:
        r = self._c.request(method, f"{self._api}{path}", **kw)
        if r.status_code >= 300:
            raise JiraError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:400]}")
        return r

    # --- identity ---
    def my_account_id(self) -> str:
        return self._req("GET", "/myself").json()["accountId"]

    # --- reads ---
    def search(self, jql: str, fields: list[str]) -> list[dict]:
        """Paginated JQL search via the current /search/jql endpoint (token-based)."""
        issues: list[dict] = []
        token: str | None = None
        while True:
            body: dict[str, object] = {"jql": jql, "fields": fields, "maxResults": 100}
            if token:
                body["nextPageToken"] = token
            data = self._req("POST", "/search/jql", json=body).json()
            issues.extend(data.get("issues", []))
            token = data.get("nextPageToken")
            if not token:
                break
        return issues

    def get_issue(self, key: str, fields: str = "", expand: str = "") -> dict:
        params = {}
        if fields:
            params["fields"] = fields
        if expand:
            params["expand"] = expand
        return self._req("GET", f"/issue/{key}", params=params).json()

    def duedate(self, key: str) -> str | None:
        return self.get_issue(key, fields="duedate")["fields"].get("duedate")

    def status_name(self, key: str) -> str:
        return self.get_issue(key, fields="status")["fields"]["status"]["name"]

    def outward_blocks(self, key: str) -> set[str]:
        """Keys this issue 'blocks' (outward Blocks links)."""
        data = self.get_issue(key, fields="issuelinks")["fields"]
        out: set[str] = set()
        for link in data.get("issuelinks", []):
            if link.get("type", {}).get("name") == BLOCKS_LINK and "outwardIssue" in link:
                out.add(link["outwardIssue"]["key"])
        return out

    # --- writes ---
    def create_epic(self, summary: str, labels: list[str], *, project: str = "RC1") -> str:
        fields = {
            "project": {"key": project},
            "issuetype": {"id": EPIC_TYPE_ID},
            "summary": summary,
            "labels": labels,
        }
        return self._req("POST", "/issue", json={"fields": fields}).json()["key"]

    def create_story(
        self,
        summary: str,
        labels: list[str],
        *,
        due: str | None = None,
        start: str | None = None,
        assignee_id: str | None = None,
        parent: str | None = None,
        project: str = "RC1",
    ) -> str:
        fields: dict[str, object] = {
            "project": {"key": project},
            "issuetype": {"id": STORY_TYPE_ID},
            "summary": summary,
            "labels": labels,
        }
        if due:
            fields["duedate"] = due
        if start:
            fields[START_DATE_FIELD] = start
        if assignee_id:
            fields["assignee"] = {"id": assignee_id}
        if parent:
            fields["parent"] = {"key": parent}
        return self._req("POST", "/issue", json={"fields": fields}).json()["key"]

    def set_priority(self, key: str, name: str) -> None:
        self._req("PUT", f"/issue/{key}", json={"fields": {"priority": {"name": name}}})

    def set_duedate(self, key: str, due: str) -> None:
        self._req("PUT", f"/issue/{key}", json={"fields": {"duedate": due}})

    def create_blocks_link(self, blocker: str, blocked: str) -> None:
        """Make `blocker` block `blocked`.

        Jira's convention here is the reverse of the intuitive reading: the
        POST ``outwardIssue`` becomes the *blocked* party ("is blocked by") and
        ``inwardIssue`` becomes the *blocker*. Verified empirically against RC1
        — see the direction check in seed_demo.verify_link_directions().
        """
        body = {
            "type": {"name": BLOCKS_LINK},
            "inwardIssue": {"key": blocker},
            "outwardIssue": {"key": blocked},
        }
        self._req("POST", "/issueLink", json=body)

    def transition_to(self, key: str, status_name: str) -> bool:
        """Transition to the named status. Returns False if already there."""
        if self.status_name(key) == status_name:
            return False
        trans = self._req("GET", f"/issue/{key}/transitions").json()["transitions"]
        match = next((t for t in trans if t["to"]["name"] == status_name), None)
        if match is None:
            raise JiraError(
                f"No transition to '{status_name}' from '{self.status_name(key)}' on {key}. "
                f"Available: {[t['to']['name'] for t in trans]}"
            )
        self._req("POST", f"/issue/{key}/transitions", json={"transition": {"id": match["id"]}})
        return True

    def delete_issue(self, key: str) -> None:
        self._req("DELETE", f"/issue/{key}")
