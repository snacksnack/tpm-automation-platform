"""Define stage tests — offline, no live API calls (RC1-302)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kpi import RUBRIC_VERSION, define
from kpi.models import Kpi, KpiTree, ShapeError, render_markdown, shape_problems

RUBRIC_DOC = Path(__file__).resolve().parent.parent / "docs" / "kpi" / "rubric.md"
BRIEFS = Path(__file__).resolve().parent.parent / "docs" / "kpi" / "programs"


class _FakeClient:
    def __init__(self, canned: dict):
        self._canned = canned
        self.captured: dict = {}
        self.calls = 0
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls += 1
        self.captured = kwargs
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(self._canned))],
            usage=SimpleNamespace(input_tokens=1200, output_tokens=900),
        )


def _kpi(id: str, type: str = "leading", **over) -> dict:
    base = {
        "id": id,
        "name": id.replace("-", " "),
        "type": type,
        "direction": "lower",
        "unit": "days",
        "definition": "forecast minus committed; undefined when throughput is zero",
        "source": {"system": "Jira", "query": "status, points", "cadence": "daily", "owner": "TPM"},
        "stale_after": "2 days",
        "so_what": "If it exceeds 5 days, the decision is cut scope.",
        "goodhart": {"risk": "low", "gaming_path": "none", "counter": "none"},
        "failure_modes": ["stale source"],
        "leads": None if type == "outcome" else {
            "outcome_id": "forecast-slip",
            "mechanism": "added points are unabsorbed work",
            "lead_time": "same day",
        },
        "activity_derived": False,
    }
    base.update(over)
    return base


def _draft(**over) -> dict:
    draft = {
        "program": "Observability Platform GA",
        "sponsor_question": "Will we hit the date and the budget, and will I hear early.",
        "outcomes": [_kpi("forecast-slip", "outcome")],
        "leading": [_kpi("scope-change"), _kpi("slack"), _kpi("blocked-share")],
        "rejected": [
            {"name": "tickets closed", "ground": "activity", "reason": "output count",
             "proxy": None, "proxy_misses": None},
        ],
        "notes": [],
    }
    draft.update(over)
    return draft


def _tree(**over) -> KpiTree:
    return KpiTree(rubric_version=1, **_draft(**over))


# --- versioning ----------------------------------------------------------------


def test_rubric_doc_and_code_agree_on_version():
    assert define.rubric_version_declared(RUBRIC_DOC.read_text()) == RUBRIC_VERSION


def test_template_declares_a_version():
    assert define.prompt_version() >= 1


def test_mismatched_rubric_version_refuses_to_draft():
    rubric = "# KPI rubric — version 99\n"
    with pytest.raises(define.DefineError, match="v99"):
        define.draft_tree("brief", rubric=rubric, client=_FakeClient(_draft()))


# --- the call -------------------------------------------------------------------


def test_draft_hands_the_model_the_brief_and_rubric_and_stamps_versions():
    client = _FakeClient(_draft())
    brief = (BRIEFS / "simulated-program.md").read_text()
    tree = define.draft_tree(brief, client=client, model="claude-test")

    assert client.calls == 1
    payload = json.loads(client.captured["messages"][0]["content"])
    assert payload["program_brief"] == brief
    assert payload["rubric"].startswith("# KPI rubric — version 1")
    assert client.captured["system"] == define.load_prompt()
    assert client.captured["output_config"]["format"]["schema"] == define.SCHEMA

    assert tree.rubric_version == RUBRIC_VERSION
    assert tree.prompt_version == define.prompt_version()
    assert tree.model == "claude-test"
    assert define.last_usage == define.CallUsage(1200, 900)


def test_briefs_do_not_leak_the_baseline():
    """The agent must not see the hand-written tree: briefs name no KPI ids."""
    for brief in BRIEFS.glob("*.md"):
        text = brief.read_text().lower()
        for leaked in ("forecast-slip", "gated-pass-rate", "planted", "week 7"):
            assert leaked not in text, f"{brief.name} mentions {leaked!r}"


# --- shape enforcement ------------------------------------------------------------


def test_shape_too_many_leading_is_refused():
    draft = _draft(leading=[_kpi(f"l{i}") for i in range(5)])
    with pytest.raises(ShapeError) as e:
        define.draft_tree("brief", client=_FakeClient(draft))
    assert any("5 leading" in p for p in e.value.problems)


def test_shape_activity_at_the_root_is_refused():
    tree = _tree(outcomes=[_kpi("tickets-closed", "outcome", activity_derived=True)])
    assert any("activity-derived" in p for p in shape_problems(tree))


def test_shape_leading_must_name_an_existing_outcome():
    bad = _kpi("orphan", leads={"outcome_id": "nope", "mechanism": "m", "lead_time": "1d"})
    tree = _tree(leading=[_kpi("a"), _kpi("b"), bad])
    assert any("orphan leads 'nope'" in p for p in shape_problems(tree))


def test_shape_high_goodhart_needs_a_counter():
    risky = _kpi("velocity", goodhart={"risk": "high", "gaming_path": "inflate", "counter": "none"})
    tree = _tree(leading=[_kpi("a"), _kpi("b"), risky])
    assert any("velocity has high Goodhart risk" in p for p in shape_problems(tree))


def test_shape_proxy_must_say_what_it_misses():
    tree = _tree(rejected=[{
        "name": "regressions caught", "ground": "unmeasurable", "reason": "no PR record",
        "proxy": "fail then pass", "proxy_misses": None,
    }])
    assert any("without saying what it misses" in p for p in shape_problems(tree))


def test_shape_reports_every_problem_at_once():
    tree = _tree(
        outcomes=[_kpi(f"o{i}", "outcome") for i in range(3)] + [_kpi("forecast-slip", "outcome")],
        leading=[_kpi("a"), _kpi("b")],
    )
    problems = shape_problems(tree)
    assert len(problems) == 2  # outcome count and leading count, together


def test_well_formed_tree_has_no_problems():
    assert shape_problems(_tree()) == []


# --- schema and models cannot drift -----------------------------------------------


def test_schema_names_exactly_the_model_fields():
    kpi_fields = set(Kpi.model_fields)
    assert set(define.SCHEMA["properties"]["outcomes"]["items"]["properties"]) == kpi_fields
    stamped = {"rubric_version", "prompt_version", "model"}
    assert set(define.SCHEMA["properties"]) == set(KpiTree.model_fields) - stamped


# --- rendering ----------------------------------------------------------------------


def test_render_is_a_document_with_every_kpi_and_rejection(tmp_path: Path):
    tree = _tree()
    tree = tree.model_copy(update={"prompt_version": 1, "model": "claude-test"})
    md = render_markdown(tree)
    assert md.startswith("# KPI tree — Observability Platform GA — agent draft")
    assert "rubric v1, define prompt v1, model `claude-test`" in md
    for k in tree.kpis:
        assert f"`{k.id}`" in md
    assert "| tickets closed | activity |" in md

    twin = define.write_tree(tree, tmp_path / "t.md")
    assert twin == tmp_path / "t.json"
    assert KpiTree.model_validate_json(twin.read_text()) == tree
