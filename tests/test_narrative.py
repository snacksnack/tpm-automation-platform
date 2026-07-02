"""Narrative digest tests — offline, no live API calls (RC1-138 acceptance)."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from collectors.models import Issue, ProjectSnapshot
from narrative.drift_digest import build_digest, build_payload, load_prompt
from narrative.models import DriftDigest
from store.models import Finding


# --- fake Anthropic client -------------------------------------------------
class _FakeClient:
    """Captures the request and returns canned structured JSON as a text block."""

    def __init__(self, canned: dict):
        self._canned = canned
        self.captured: dict = {}
        self.calls = 0
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls += 1
        self.captured = kwargs
        text = json.dumps(self._canned)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


class _ExplodingClient:
    def __init__(self):
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        raise AssertionError("API must not be called")


def _issue(key: str, summary: str, **kw) -> Issue:
    return Issue(
        key=key, summary=summary, status=kw.get("status", "In Progress"),
        status_category="In Progress", priority="High",
        assignee_name=kw.get("owner"), due=kw.get("due"), start=kw.get("start"),
    )


def _snapshot() -> ProjectSnapshot:
    return ProjectSnapshot(
        project_key="RC1",
        issues=[
            _issue("RC1-1", "Vendor security review", owner="Reid", due=date(2026, 7, 23)),
            _issue("RC1-2", "Launch readiness", owner="Dana", start=date(2026, 7, 24)),
            _issue("RC1-9", "Analytics dashboard", owner="Kim", start=date(2026, 7, 30)),
        ],
    )


def _finding(rule, down, up=None, sev=10.0, bucket="yellow", run=5, first=5) -> Finding:
    return Finding(
        rule_type=rule, downstream=down, upstream=up, severity=sev,
        severity_bucket=bucket, detail=f"{up or '?'} -> {down} drift", run_id=run,
        first_seen_run=first,
    )


# --- empty findings --------------------------------------------------------
def test_empty_findings_all_clear_without_api_call():
    digest = build_digest([], _snapshot(), client=_ExplodingClient())
    assert isinstance(digest, DriftDigest)
    assert digest.all_clear is True
    assert digest.findings == []
    assert "\n" not in digest.summary  # one-line all-clear
    assert "RC1" in digest.subject


def test_empty_findings_mentions_resolved():
    resolved = [_finding("unabsorbed_slip", "RC1-2", "RC1-1")]
    digest = build_digest([], _snapshot(), resolved=resolved, client=_ExplodingClient())
    assert "resolved" in digest.summary.lower()


# --- payload fidelity (what the model is handed) ---------------------------
def test_payload_enriches_tickets_and_counts():
    findings = [
        _finding("unabsorbed_slip", "RC1-2", "RC1-1", sev=28.0, bucket="red", run=None, first=None),
        _finding("transitive_risk", "RC1-9", "RC1-1", sev=2.0, bucket="white"),
    ]
    payload = build_payload(findings, _snapshot())

    assert payload["project"] == "RC1"
    assert payload["counts"] == {"red": 1, "yellow": 0, "white": 1, "new": 2}
    # Sorted most-severe first.
    assert [f["bucket"] for f in payload["findings"]] == ["red", "white"]
    top = payload["findings"][0]
    assert top["downstream"]["key"] == "RC1-2"
    assert top["downstream"]["summary"] == "Launch readiness"
    assert top["downstream"]["owner"] == "Dana"
    assert top["upstream"]["summary"] == "Vendor security review"
    assert top["upstream"]["due"] == "2026-07-23"


def test_payload_new_flag_true_for_unpersisted():
    fresh = _finding("lead_time_risk", "RC1-9", run=None, first=None)
    payload = build_payload([fresh], _snapshot())
    assert payload["findings"][0]["is_new"] is True


# --- full build with a fake client -----------------------------------------
def test_build_digest_parses_structured_output():
    canned = {
        "subject": "RC1 drift: launch at risk",
        "summary": "Vendor review slipped. 1 red, 1 white.",
        "findings": [
            {"downstream": "RC1-2", "bucket": "red", "line": "🔴 RC1-2 ..."},
            {"downstream": "RC1-9", "bucket": "white", "line": "⚪ RC1-9 ..."},
        ],
    }
    client = _FakeClient(canned)
    findings = [
        _finding("unabsorbed_slip", "RC1-2", "RC1-1", sev=28.0, bucket="red"),
        _finding("transitive_risk", "RC1-9", "RC1-1", sev=2.0, bucket="white"),
    ]
    digest = build_digest(findings, _snapshot(), client=client)

    assert client.calls == 1
    assert digest.all_clear is False
    assert digest.subject == canned["subject"]
    assert [line.downstream for line in digest.findings] == ["RC1-2", "RC1-9"]
    assert digest.findings[0].bucket == "red"

    # Structured output + correct model requested, and real facts sent (no fabrication surface).
    assert client.captured["model"] == "claude-opus-4-8"
    assert client.captured["output_config"]["format"]["type"] == "json_schema"
    sent = json.loads(client.captured["messages"][0]["content"])
    assert sent["findings"][0]["downstream"]["key"] == "RC1-2"


# --- prompt template -------------------------------------------------------
def test_prompt_template_carries_guardrails():
    prompt = load_prompt()
    lowered = prompt.lower()
    assert "never invent" in lowered
    assert "one line per finding" in lowered
    assert "2-sentence" in lowered
    assert "soften" in lowered  # never soften severity


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
