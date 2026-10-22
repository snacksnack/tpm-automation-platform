"""Jira CLI — a thin, token-cheap wrapper over seed.jira_client.JiraClient (RC1-538).

Standardizes the platform's day-to-day Jira work on the REST API rather than the
MCP connector: narrow field masks and compact output cost far fewer tokens, and
REST is more reliable in agent sessions. This is dev/ops tooling and intentionally
lives outside the deployed image (seed/ is not copied into the Dockerfile); it
reuses the same JiraClient the seed and simulate flows use, and reads credentials
from config.settings. It never runs on a schedule — writes are CLI-only.

    uv run python -m seed.jira_cli get RC1-538
    uv run python -m seed.jira_cli get RC1-538 --fields summary,status --raw
    uv run python -m seed.jira_cli search 'project=RC1 AND statusCategory != Done' --limit 20
    uv run python -m seed.jira_cli transition RC1-538 "In Review"
    uv run python -m seed.jira_cli comment RC1-538 "PR up for review"
    uv run python -m seed.jira_cli create-story "Short summary" --parent RC1-537 --desc "..."
"""
from __future__ import annotations

import argparse
import json
import sys

from config import settings
from seed.jira_client import JiraClient, JiraError

# Compact default field set for `get` — enough to triage an issue without the ADF bulk.
DEFAULT_GET_FIELDS = "summary,status,issuetype,parent,assignee,labels,priority,description"
DEFAULT_SEARCH_FIELDS = "summary,status"

# ADF block nodes after which a line break keeps lists and paragraphs readable.
_ADF_BLOCKS = {"paragraph", "heading", "listItem", "blockquote", "codeBlock"}


def _jira() -> JiraClient:
    return JiraClient(
        settings.jira_base_url, settings.jira_email or "", settings.jira_api_token or ""
    )


def _adf_to_text(node: object) -> str:
    """Flatten an Atlassian Document Format fragment to plain text. Best-effort:
    text nodes concatenate; block nodes add a line break. Unknown node types are
    walked for their content, so new marks/extensions degrade to their text."""
    if not isinstance(node, dict):
        return ""
    parts: list[str] = []
    if node.get("type") == "text":
        parts.append(str(node.get("text", "")))
    for child in node.get("content") or []:
        parts.append(_adf_to_text(child))
    text = "".join(parts)
    if node.get("type") in _ADF_BLOCKS:
        text += "\n"
    return text


def _name(value: object) -> str:
    """Pull the human name out of a Jira field that may be a dict or a scalar."""
    if isinstance(value, dict):
        return str(value.get("name") or value.get("displayName") or "")
    return str(value) if value is not None else ""


def _format_issue(issue: dict) -> str:
    fields = issue.get("fields") or {}
    out = [f"{issue.get('key', '?')}  {fields.get('summary', '')}".rstrip()]
    rows: list[tuple[str, str]] = []
    if fields.get("issuetype"):
        rows.append(("Type", _name(fields["issuetype"])))
    if fields.get("status"):
        rows.append(("Status", _name(fields["status"])))
    parent = fields.get("parent")
    if parent:
        psum = (parent.get("fields") or {}).get("summary", "")
        rows.append(("Parent", f"{parent.get('key', '')} {psum}".strip()))
    if fields.get("assignee"):
        rows.append(("Assignee", _name(fields["assignee"])))
    if fields.get("priority"):
        rows.append(("Priority", _name(fields["priority"])))
    if fields.get("labels"):
        rows.append(("Labels", ", ".join(fields["labels"])))
    out += [f"  {label}: {value}" for label, value in rows]
    description = fields.get("description")
    if description:
        text = _adf_to_text(description).strip()
        if text:
            out.append("  ---")
            out += [f"  {line}" for line in text.splitlines()]
    return "\n".join(out)


def _cmd_get(jira: JiraClient, args: argparse.Namespace) -> int:
    issue = jira.get_issue(args.key, fields=args.fields)
    if args.raw:
        print(json.dumps(issue, indent=2, ensure_ascii=False))
    else:
        print(_format_issue(issue))
    return 0


def _cmd_search(jira: JiraClient, args: argparse.Namespace) -> int:
    fields = [f.strip() for f in args.fields.split(",") if f.strip()]
    issues = jira.search(args.jql, fields)
    if args.limit:
        issues = issues[: args.limit]
    for issue in issues:
        f = issue.get("fields") or {}
        print(f"{issue.get('key', '?')} | {_name(f.get('status')):<12} | {f.get('summary', '')}")
    print(f"({len(issues)} issue(s))", file=sys.stderr)
    return 0


def _cmd_transition(jira: JiraClient, args: argparse.Namespace) -> int:
    ok = jira.transition_to(args.key, args.status)
    if ok:
        print(f"{args.key} -> {args.status}")
        return 0
    print(f"{args.key}: no transition to {args.status!r} available", file=sys.stderr)
    return 1


def _cmd_comment(jira: JiraClient, args: argparse.Namespace) -> int:
    jira.add_comment(args.key, args.text)
    print(f"{args.key}: comment added")
    return 0


def _cmd_create_story(jira: JiraClient, args: argparse.Namespace) -> int:
    key = jira.create_story(
        args.summary,
        args.label or [],
        parent=args.parent,
        description=args.desc,
        project=args.project,
    )
    print(key)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="jira", description="Token-cheap Jira CLI over the REST API (RC1-538)."
    )
    sub = ap.add_subparsers(dest="command", required=True)

    g = sub.add_parser("get", help="show one issue (compact)")
    g.add_argument("key")
    g.add_argument("--fields", default=DEFAULT_GET_FIELDS, help="comma-separated field mask")
    g.add_argument("--raw", action="store_true", help="print the raw issue JSON")
    g.set_defaults(func=_cmd_get)

    s = sub.add_parser("search", help="JQL search, one line per issue")
    s.add_argument("jql")
    s.add_argument("--fields", default=DEFAULT_SEARCH_FIELDS, help="comma-separated field mask")
    s.add_argument("--limit", type=int, default=None, help="cap rows printed")
    s.set_defaults(func=_cmd_search)

    t = sub.add_parser("transition", help="move an issue to a status by name")
    t.add_argument("key")
    t.add_argument("status")
    t.set_defaults(func=_cmd_transition)

    c = sub.add_parser("comment", help="add a comment")
    c.add_argument("key")
    c.add_argument("text")
    c.set_defaults(func=_cmd_comment)

    cs = sub.add_parser("create-story", help="create a Story")
    cs.add_argument("summary")
    cs.add_argument("--parent", default=None, help="epic key to parent under")
    cs.add_argument("--desc", default=None, help="plain-text description")
    cs.add_argument("--label", action="append", help="label (repeatable)")
    cs.add_argument("--project", default="RC1")
    cs.set_defaults(func=_cmd_create_story)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        with _jira() as jira:
            return args.func(jira, args)
    except JiraError as e:
        print(f"Jira error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
