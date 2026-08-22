"""Define stage: program brief + rubric -> a reviewable KPI tree (RC1-302).

The model drafts; the code enforces the rubric's shape. Same split as the
drift digest: the prompt is a versioned template, the output is structured
(json_schema) so the instrument stage can consume it, and the deterministic
checks in `kpi.models.shape_problems` run on every draft — a tree with five
leading indicators or an outcome that is an activity count is refused here,
not discovered in review.

What the model is *not* given: the hand-written baseline. The review
(docs/kpi/trees/<program>.review.md) compares the two afterwards; if the agent
saw the baseline, agreement would measure copying.

Usage:
    python -m kpi.define --program docs/kpi/programs/simulated-program.md \
        --out docs/kpi/trees/simulated-program.agent.md
    python -m kpi.define --program ... --out ... --model claude-sonnet-5

Writes the markdown document and a `.json` twin beside it. Needs
ANTHROPIC_API_KEY (config reads .env). Spends money — a draft is one call.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from kpi import RUBRIC_VERSION
from kpi.models import KpiTree, ShapeError, render_markdown, validate_shape

MODEL = "claude-opus-4-8"
_TEMPLATE = Path(__file__).parent / "templates" / "define.md"
RUBRIC = Path(__file__).resolve().parent.parent / "docs" / "kpi" / "rubric.md"

_VERSION_COMMENT = re.compile(r"version\s+(\d+)", re.IGNORECASE)


class DefineError(RuntimeError):
    pass


def load_prompt() -> str:
    return _TEMPLATE.read_text()


def prompt_version(text: str | None = None) -> int:
    """The hand-maintained version in the template's leading HTML comment."""
    head = (text if text is not None else load_prompt()).splitlines()[0]
    m = _VERSION_COMMENT.search(head)
    if not m:
        raise DefineError("define.md has no version comment on its first line")
    return int(m.group(1))


def rubric_version_declared(text: str | None = None) -> int:
    """The version in the rubric's title — the document is the authority."""
    head = (text if text is not None else RUBRIC.read_text()).splitlines()[0]
    m = _VERSION_COMMENT.search(head)
    if not m:
        raise DefineError("rubric.md has no version in its title")
    return int(m.group(1))


# --- structured output schema -----------------------------------------------
# Hand-written rather than derived from the pydantic models so the API gets a
# closed schema (every object additionalProperties: false, every field
# required). A test asserts it names exactly the fields the models do, so the
# two cannot drift apart silently.

_SOURCE = {
    "type": "object",
    "properties": {
        "system": {"type": "string"},
        "query": {"type": "string"},
        "cadence": {"type": "string"},
        "owner": {"type": "string"},
    },
    "required": ["system", "query", "cadence", "owner"],
    "additionalProperties": False,
}
_LEADS = {
    "type": "object",
    "properties": {
        "outcome_id": {"type": "string"},
        "mechanism": {"type": "string"},
        "lead_time": {"type": "string"},
    },
    "required": ["outcome_id", "mechanism", "lead_time"],
    "additionalProperties": False,
}
_GOODHART = {
    "type": "object",
    "properties": {
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "gaming_path": {"type": "string"},
        "counter": {"type": "string"},
    },
    "required": ["risk", "gaming_path", "counter"],
    "additionalProperties": False,
}
_KPI = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "type": {"type": "string", "enum": ["outcome", "leading"]},
        "direction": {"type": "string", "enum": ["higher", "lower"]},
        "unit": {"type": "string"},
        "definition": {"type": "string"},
        "source": _SOURCE,
        "stale_after": {"type": "string"},
        "so_what": {"type": "string"},
        "goodhart": _GOODHART,
        "failure_modes": {"type": "array", "items": {"type": "string"}},
        "leads": {"anyOf": [_LEADS, {"type": "null"}]},
        "activity_derived": {"type": "boolean"},
    },
    "required": [
        "id", "name", "type", "direction", "unit", "definition", "source", "stale_after",
        "so_what", "goodhart", "failure_modes", "leads", "activity_derived",
    ],
    "additionalProperties": False,
}
_REJECTED = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "ground": {
            "type": "string",
            "enum": ["activity", "no-decision", "unmeasurable", "duplicate", "diagnostic"],
        },
        "reason": {"type": "string"},
        "proxy": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "proxy_misses": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["name", "ground", "reason", "proxy", "proxy_misses"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "program": {"type": "string"},
        "sponsor_question": {"type": "string"},
        "outcomes": {"type": "array", "items": _KPI},
        "leading": {"type": "array", "items": _KPI},
        "rejected": {"type": "array", "items": _REJECTED},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["program", "sponsor_question", "outcomes", "leading", "rejected", "notes"],
    "additionalProperties": False,
}


def build_payload(brief: str, rubric: str) -> dict:
    """Exactly what the model sees: the brief and the rubric, nothing derived."""
    return {"rubric": rubric, "program_brief": brief}


@dataclass(frozen=True)
class CallUsage:
    input_tokens: int
    output_tokens: int


#: Token usage of the most recent `draft_tree` call, for callers that meter
#: spend (same side-channel shape as narrative.drift_digest).
last_usage: CallUsage | None = None


def _default_client():
    import anthropic  # lazy: tests and offline paths need no SDK auth

    from config import settings

    return anthropic.Anthropic(
        api_key=settings.anthropic_api_key or None, timeout=180.0, max_retries=3
    )


def _generate(client, model: str, system: str, payload: dict) -> dict:
    global last_usage
    resp = client.messages.create(
        model=model,
        max_tokens=8192,
        system=system,
        messages=[{"role": "user", "content": json.dumps(payload)}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
    )
    usage = getattr(resp, "usage", None)
    last_usage = CallUsage(
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
    )
    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
    if text is None:
        raise DefineError("model returned no text block")
    return json.loads(text)


def draft_tree(
    brief: str,
    *,
    rubric: str | None = None,
    client=None,
    model: str = MODEL,
) -> KpiTree:
    """Brief -> validated KpiTree. Raises ShapeError when the draft breaks the rubric's shape.

    The rubric version is stamped from the document, not from the constant, so
    a tree always records the rules it was actually drafted under.
    """
    global last_usage
    last_usage = None
    rubric_text = rubric if rubric is not None else RUBRIC.read_text()
    declared = rubric_version_declared(rubric_text)
    if declared != RUBRIC_VERSION:
        raise DefineError(
            f"rubric.md declares v{declared} but kpi.RUBRIC_VERSION is {RUBRIC_VERSION}; "
            "bump both together"
        )
    system = load_prompt()
    data = _generate(client or _default_client(), model, system, build_payload(brief, rubric_text))
    tree = KpiTree(
        rubric_version=declared, prompt_version=prompt_version(system), model=model, **data
    )
    return validate_shape(tree)


def write_tree(tree: KpiTree, out: Path) -> Path:
    """Markdown at `out`, JSON twin beside it. Returns the JSON path."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(tree) + "\n")
    twin = out.with_suffix(".json")
    twin.write_text(tree.model_dump_json(indent=2) + "\n")
    return twin


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Draft a KPI tree for a program brief.")
    ap.add_argument("--program", required=True, type=Path, help="program brief (markdown)")
    ap.add_argument("--out", required=True, type=Path, help="where to write the tree (markdown)")
    ap.add_argument("--model", default=MODEL)
    args = ap.parse_args(argv)

    from config import settings

    if not settings.anthropic_api_key:
        print("ANTHROPIC_API_KEY is not set (config reads .env).", file=sys.stderr)
        return 2
    brief = args.program.read_text()
    try:
        tree = draft_tree(brief, model=args.model)
    except ShapeError as e:
        print("draft refused — rubric shape problems:", file=sys.stderr)
        for p in e.problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    twin = write_tree(tree, args.out)
    u = last_usage
    spend = f" ({u.input_tokens} in / {u.output_tokens} out tokens)" if u else ""
    print(
        f"wrote {args.out} and {twin.name}: {len(tree.outcomes)} outcome(s), "
        f"{len(tree.leading)} leading, {len(tree.rejected)} rejected{spend}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
