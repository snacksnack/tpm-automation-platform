"""Offline tests for the Jira CLI (RC1-538). No network: a fake client stands in
for JiraClient via the monkeypatched _jira() factory, the same pattern simulate
and the collectors use."""
from __future__ import annotations

import pytest

from seed import jira_cli as cli


def _adf(text: str) -> dict:
    para = {"type": "paragraph", "content": [{"type": "text", "text": text}]}
    return {"type": "doc", "content": [para]}


class FakeJira:
    def __init__(self) -> None:
        self.comments: list[tuple[str, str]] = []
        self.created: list[dict] = []
        self.transitions: list[tuple[str, str]] = []
        self.transition_result = True

    def get_issue(self, key: str, fields: str = "", expand: str = "") -> dict:
        self.last_fields = fields
        return {
            "key": key,
            "fields": {
                "summary": "A thing",
                "status": {"name": "In Progress"},
                "issuetype": {"name": "Story"},
                "parent": {"key": "RC1-537", "fields": {"summary": "Epic"}},
                "assignee": {"displayName": "Reid Collins"},
                "labels": ["dx"],
                "description": _adf("Line one.\nLine two."),
            },
        }

    def search(self, jql: str, fields: list[str]) -> list[dict]:
        self.last_jql = jql
        return [
            {"key": "RC1-1", "fields": {"summary": "First", "status": {"name": "Done"}}},
            {"key": "RC1-2", "fields": {"summary": "Second", "status": {"name": "To Do"}}},
        ]

    def transition_to(self, key: str, status: str) -> bool:
        self.transitions.append((key, status))
        return self.transition_result

    def add_comment(self, key: str, text: str) -> None:
        self.comments.append((key, text))

    def create_story(self, summary, labels, *, parent=None, description=None, project="RC1", **kw):
        self.created.append(
            {
                "summary": summary,
                "labels": labels,
                "parent": parent,
                "desc": description,
                "project": project,
            }
        )
        return "RC1-999"


@pytest.fixture
def fake(monkeypatch):
    client = FakeJira()

    class _Ctx:
        def __enter__(self):
            return client

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(cli, "_jira", lambda: _Ctx())
    return client


def test_get_compact(fake, capsys):
    assert cli.main(["get", "RC1-538"]) == 0
    out = capsys.readouterr().out
    assert "RC1-538  A thing" in out
    assert "Status: In Progress" in out
    assert "Parent: RC1-537 Epic" in out
    assert "Line one." in out and "Line two." in out  # ADF flattened to text
    assert fake.last_fields == cli.DEFAULT_GET_FIELDS


def test_get_raw_is_json(fake, capsys):
    assert cli.main(["get", "RC1-538", "--raw", "--fields", "summary"]) == 0
    out = capsys.readouterr().out
    assert '"key": "RC1-538"' in out
    assert fake.last_fields == "summary"


def test_search_one_line_per_issue_and_limit(fake, capsys):
    assert cli.main(["search", "project = RC1", "--limit", "1"]) == 0
    cap = capsys.readouterr()
    lines = [ln for ln in cap.out.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("RC1-1 | Done")
    assert fake.last_jql == "project = RC1"


def test_transition_ok_and_missing(fake, capsys):
    assert cli.main(["transition", "RC1-538", "In Review"]) == 0
    assert fake.transitions == [("RC1-538", "In Review")]
    fake.transition_result = False
    assert cli.main(["transition", "RC1-538", "Nope"]) == 1


def test_comment_and_create_story(fake, capsys):
    assert cli.main(["comment", "RC1-538", "hello"]) == 0
    assert fake.comments == [("RC1-538", "hello")]
    assert cli.main(["create-story", "New thing", "--parent", "RC1-537", "--desc", "why"]) == 0
    assert capsys.readouterr().out.strip().endswith("RC1-999")
    assert fake.created[0]["parent"] == "RC1-537"
    assert fake.created[0]["desc"] == "why"


def test_jira_error_returns_1(monkeypatch, capsys):
    def _boom():
        raise cli.JiraError("JIRA_EMAIL and JIRA_API_TOKEN must be set")

    monkeypatch.setattr(cli, "_jira", _boom)
    assert cli.main(["get", "RC1-1"]) == 1
    assert "Jira error" in capsys.readouterr().err
