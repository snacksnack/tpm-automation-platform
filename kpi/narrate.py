"""Narrate: the weekly SVP brief, written about the readings (RC1-306).

The fifth stage. Track lands the numbers; this writes the week's prose —
trend, what moved, what it means, asks — and posts it to Slack. The epic's
"deterministic numbers, LLM narrative" line runs through the middle of this
module: `build_payload` is ordinary Python over stored readings and hands
the model every number it is allowed to say, and `audit_numbers` refuses
any brief whose prose contains a number the payload does not — the model
writes about the readings, it never computes one.

What the sponsor sees is shaped by the earlier stages, not by this one:

- **Outcomes first, and only outcomes above the fold.** The tree (RC1-302)
  labelled every KPI outcome or leading; the payload keeps that order and
  the renderer prints outcome lines before the movement paragraph, where
  leading indicators are allowed to appear as explanation.
- **A KPI that could not be measured says so.** Stale and broken readings
  arrive with their reasons and are labelled in the brief; a proxied KPI
  carries the instrument stage's caveat (RC1-303) on every line it appears
  in. Never a zero, never narrated around.
- **Every number traces to its snapshot.** The brief is archived beside the
  readings (`kpi/briefs_store.py`) with the exact payload the model saw and
  the `run_id` its numbers came from — brief -> payload -> `python -m
  collectors show <program> --run N` is the whole audit trail.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from collectors.programs import Program
from config import settings
from kpi import instrument, track
from kpi.briefs_store import Brief, BriefsStore
from kpi.define import KpiTree
from kpi.escalations_store import EscalationsStore
from kpi.instrument import Instrumentation
from kpi.readings_store import ReadingsStore, StoredReading

MODEL = "claude-opus-5"
_TEMPLATE = Path(__file__).parent / "templates" / "narrate.md"
_VERSION_COMMENT = re.compile(r"version\s+(\d+)", re.IGNORECASE)

SERIES_DAYS = 28  # daily readings shown to the model, ending at the week
WEEK_DAYS = 7
SMALL_COUNT_CEILING = 12  # counting words ("three KPIs") the audit lets through


class NarrateError(RuntimeError):
    pass


def load_prompt() -> str:
    return _TEMPLATE.read_text()


def prompt_version(text: str | None = None) -> int:
    """The hand-maintained version in the template's leading HTML comment."""
    head = (text if text is not None else load_prompt()).splitlines()[0]
    m = _VERSION_COMMENT.search(head)
    if not m:
        raise NarrateError("narrate.md has no version comment on its first line")
    return int(m.group(1))


# --- structured output schema ----------------------------------------------------------------
# Hand-written, closed, like define/instrument: the model returns prose fields
# only — every number it may use is already in the payload it was shown.

SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "outcome_lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kpi_id": {"type": "string"},
                    "line": {"type": "string"},
                },
                "required": ["kpi_id", "line"],
                "additionalProperties": False,
            },
        },
        "movement": {"type": "string"},
        "asks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "outcome_lines", "movement", "asks"],
    "additionalProperties": False,
}


# --- the payload: every number the model is allowed to say -----------------------------------


def _reading_dict(sr: StoredReading) -> dict:
    r = sr.reading
    return {
        "sim_date": r.sim_date.isoformat(),
        "value": r.value,
        "state": r.state,
        "tripped": r.tripped,
        "as_of": r.as_of.isoformat() if r.as_of else None,
        "reason": r.reason,
        "detail": r.detail,
        "run_id": sr.run_id,
    }


def _week_ago(series: list[StoredReading], week_ending: date) -> StoredReading | None:
    """The latest reading at least a week older than the one being narrated."""
    target = week_ending - timedelta(days=WEEK_DAYS)
    earlier = [sr for sr in series if sr.reading.sim_date <= target]
    return earlier[-1] if earlier else None


def _kpi_entry(kpi, kind: str, series: list[StoredReading], week_ending: date, inst_kpi) -> dict:
    latest = series[-1]
    before = _week_ago(series, week_ending)
    now_r, then_r = latest.reading, before.reading if before else None
    delta = (
        round(now_r.value - then_r.value, 4)
        if then_r is not None and now_r.value is not None and then_r.value is not None
        else None
    )
    window_floor = week_ending - timedelta(days=SERIES_DAYS)
    return {
        "kpi_id": kpi.id,
        "name": kpi.name,
        "kind": kind,
        "unit": kpi.unit,
        "direction": kpi.direction,
        "so_what": kpi.so_what,
        "leads": kpi.leads.model_dump() if kpi.leads else None,
        "latest": _reading_dict(latest),
        "week_ago": (
            {"sim_date": then_r.sim_date.isoformat(), "value": then_r.value,
             "state": then_r.state, "tripped": then_r.tripped}
            if then_r is not None
            else None
        ),
        "delta": delta,
        "series": [
            {"sim_date": sr.reading.sim_date.isoformat(), "value": sr.reading.value,
             "state": sr.reading.state}
            for sr in series
            if sr.reading.sim_date > window_floor
        ],
        "newly_tripped": now_r.tripped and not (then_r.tripped if then_r else False),
        "newly_broken": now_r.state == "broken"
        and (then_r.state != "broken" if then_r else True),
        "recovered": now_r.state == "ok"
        and not now_r.tripped
        and then_r is not None
        and (then_r.state != "ok" or then_r.tripped),
        "proxy": inst_kpi.proxy if inst_kpi else None,
        "caveat": inst_kpi.caveat if inst_kpi else None,
    }


def escalation_entry(esc) -> dict:
    """One escalation (RC1-307) as the payload carries it — the stage's own
    words, numbers included, so the audit can vouch for them."""
    return {
        "sim_date": esc.sim_date.isoformat(),
        "kind": esc.kind,
        "subject": esc.subject,
        "kpi_ids": list(esc.kpi_ids),
        "reason": esc.reason,
        "proposed_fix": esc.proposed_fix,
        "healed": esc.healed,
    }


def build_payload(
    program: Program,
    tree: KpiTree,
    inst: Instrumentation,
    stored: list[StoredReading],
    escalations: list[dict] | None = None,
) -> dict:
    """One week as JSON, outcomes first — the model's entire world.

    Deterministic and number-complete: everything the brief may cite is in
    here, which is what makes the audit a real gate rather than a hope.
    """
    if not stored:
        raise NarrateError(f"{program.id}: no readings stored; nothing to narrate")
    by_kpi: dict[str, list[StoredReading]] = {}
    for sr in sorted(stored, key=lambda s: (s.reading.sim_date, s.reading.kpi_id)):
        by_kpi.setdefault(sr.reading.kpi_id, []).append(sr)
    week_ending = max(sr.reading.sim_date for sr in stored)
    shipping = set(inst.computes)
    inst_by_id = {k.kpi_id: k for k in inst.kpis}

    kpis: list[dict] = []
    not_shipping: list[dict] = []
    for kind, group in (("outcome", tree.outcomes), ("leading", tree.leading)):
        for kpi in group:
            series = by_kpi.get(kpi.id, [])
            if kpi.id not in shipping or not series:
                verdict = inst_by_id.get(kpi.id)
                why = (
                    f"instrumented {verdict.status}" if verdict else "not instrumented"
                ) if kpi.id not in shipping else "shipping but no reading stored yet"
                not_shipping.append({"kpi_id": kpi.id, "kind": kind, "why": why})
                continue
            kpis.append(_kpi_entry(kpi, kind, series, week_ending, inst_by_id.get(kpi.id)))

    if not kpis:
        raise NarrateError(f"{program.id}: no shipping KPI has a stored reading")
    return {
        "program_id": program.id,
        "program": program.name,
        "week_ending": week_ending.isoformat(),
        "run_id": max(
            sr.run_id for sr in stored if sr.reading.sim_date == week_ending
        ),
        "kpis": kpis,
        "not_shipping": not_shipping,
        "escalations": escalations or [],
    }


# --- the audit: the model's prose may not contain a number the payload does not --------------

_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _allowed_numbers(payload: object, out: set[float]) -> None:
    if isinstance(payload, bool):
        return
    if isinstance(payload, (int, float)):
        out.add(round(float(abs(payload)), 6))
    elif isinstance(payload, str):
        # Payload strings are quotable too: a reading's detail ("spec-review
        # 50 %"), a reason, a so-what threshold. The model saw them; repeating
        # them is not inventing.
        for m in _DATE.finditer(payload):
            y, mo, d = m.group(0).split("-")
            out.update({float(y), float(mo), float(d)})
        for m in _NUMBER.finditer(_DATE.sub(" ", payload)):
            out.add(round(abs(float(m.group(0).replace(",", ""))), 6))
    elif isinstance(payload, dict):
        for v in payload.values():
            _allowed_numbers(v, out)
    elif isinstance(payload, list):
        for v in payload:
            _allowed_numbers(v, out)


def _matches(token: float, decimals: int, allowed: set[float]) -> bool:
    if token in allowed:
        return True
    if token <= SMALL_COUNT_CEILING and token == int(token):
        return True
    # A payload number quoted rounded to fewer decimals ("2.04" for 2.0417).
    return any(round(a, decimals) == token for a in allowed)


def audit_numbers(texts: list[str], payload: dict) -> list[str]:
    """Numbers in the model's prose that the payload cannot vouch for.

    Anything returned is grounds to refuse the brief: the numbers come from
    the track stage unchanged, and a number the payload does not contain is
    by definition one the model made up.
    """
    allowed: set[float] = set()
    _allowed_numbers(payload, allowed)
    bad: list[str] = []
    for text in texts:
        cleaned = _DATE.sub(" ", text)
        for m in _NUMBER.finditer(cleaned):
            raw = m.group(0)
            token = abs(float(raw.replace(",", "")))
            decimals = len(raw.rsplit(".", 1)[1]) if "." in raw else 0
            if not _matches(round(token, 6), decimals, allowed):
                bad.append(raw)
    return bad


def _narrative_texts(data: dict) -> list[str]:
    return [
        data["headline"],
        *[line["line"] for line in data["outcome_lines"]],
        data["movement"],
        *data["asks"],
    ]


def _clean(text: str) -> str:
    """Strip structural-output artifacts (stray braces, ragged whitespace) the
    model occasionally leaves in a prose field. Words only — numbers untouched."""
    return re.sub(r"\s+", " ", text.replace("{", "").replace("}", "")).strip()


def _cleaned(data: dict) -> dict:
    return {
        "headline": _clean(data["headline"]),
        "outcome_lines": [
            {"kpi_id": line["kpi_id"], "line": _clean(line["line"])}
            for line in data["outcome_lines"]
        ],
        "movement": _clean(data["movement"]),
        "asks": [_clean(ask) for ask in data["asks"] if _clean(ask)],
    }


# --- rendering: numbers by code, prose by the model ------------------------------------------


def _short_unit(unit: str) -> str:
    if unit.lstrip().startswith("%"):
        return " %"
    if unit.lower().startswith("days"):
        return " days"
    return ""


def _fmt_value(entry: dict) -> str:
    v = entry["latest"]["value"]
    if v is None:
        return "—"
    prefix = "$" if "USD" in entry["unit"] else ""
    return f"{prefix}{v:g}{_short_unit(entry['unit'])}"


def _fmt_delta(entry: dict) -> str:
    if entry["week_ago"] is None:
        return "first week"
    d = entry["delta"]
    if d is None:
        return "no prior value"
    return f"{'+' if d >= 0 else ''}{d:g} w/w"


def _mark(entry: dict) -> str:
    latest = entry["latest"]
    if latest["state"] == "broken":
        return "🔴"
    if latest["state"] == "stale":
        return "🟡"
    return "🔴" if latest["tripped"] else "🟢"


def _tags(entry: dict) -> str:
    latest, tags = entry["latest"], []
    if latest["state"] != "ok":
        tags.append(f"[{latest['state']}: {latest['reason']}]")
    if entry["proxy"]:
        tags.append(f"[proxy: {entry['caveat'] or entry['proxy']}]")
    return (" " + " ".join(tags)) if tags else ""


def render_brief(payload: dict, data: dict) -> str:
    """The Slack message: one screen, outcomes first, every number from the
    payload and rendered by this function — the model's prose sits beside
    them, never in place of them."""
    lines_by_id = {line["kpi_id"]: line["line"] for line in data["outcome_lines"]}
    out = [
        f"*Weekly KPI brief — {payload['program']} — week ending {payload['week_ending']}*",
        data["headline"],
        "",
        "*Outcomes*",
    ]
    for entry in payload["kpis"]:
        if entry["kind"] != "outcome":
            continue
        prose = lines_by_id.get(entry["kpi_id"], "")
        out.append(
            f"{_mark(entry)} *{entry['name']}*: {_fmt_value(entry)} ({_fmt_delta(entry)})"
            + (f" — {prose}" if prose else "")
            + _tags(entry)
        )
    out += ["", "*What moved — leading indicators*", data["movement"]]
    if data["asks"]:
        out += ["", "*Asks*"]
        out += [f"• {ask}" for ask in data["asks"]]
    if payload["not_shipping"]:
        gaps = "; ".join(f"{g['kpi_id']} ({g['why']})" for g in payload["not_shipping"])
        out += ["", f"_Not in this brief: {gaps}._"]
    if payload.get("escalations"):
        out += ["", "*Escalations this week*"]
        for esc in payload["escalations"]:
            mark = "🟢 healed" if esc["healed"] else "🔴"
            radius = ", ".join(f"`{k}`" for k in esc["kpi_ids"])
            out.append(
                f"{mark} {esc['sim_date']} *{esc['kind']}: {esc['subject']}* — {esc['reason']} "
                f"(blast radius: {radius}) — fix: {esc['proposed_fix']}"
            )
    out += [
        "",
        f"_Numbers: track stage, snapshot run {payload['run_id']}, sim-date "
        f"{payload['week_ending']} — `kpi_readings` holds every value unchanged; the model "
        "wrote prose only. Stale, broken and proxied KPIs are labelled._",
    ]
    return "\n".join(out)


# --- the model call --------------------------------------------------------------------------


@dataclass(frozen=True)
class CallUsage:
    input_tokens: int
    output_tokens: int


last_usage: CallUsage | None = None


def _default_client():
    key = settings.anthropic_api_key
    if not key:
        raise NarrateError("ANTHROPIC_API_KEY is not set (config reads .env)")
    import anthropic

    return anthropic.Anthropic(api_key=key, timeout=120.0, max_retries=3)


def _generate(client, model: str, system: str, payload: dict) -> dict:
    global last_usage
    resp = client.messages.create(
        model=model,
        max_tokens=4096,
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
        raise NarrateError("model returned no text block")
    return json.loads(text)


def narrate(
    program: Program,
    tree: KpiTree,
    inst: Instrumentation,
    stored: list[StoredReading],
    *,
    escalations: list[dict] | None = None,
    client=None,
    model: str = MODEL,
) -> Brief:
    """Readings -> archived-shape brief. Raises NarrateError when the model's
    prose contains a number the payload cannot vouch for — a rejected brief
    is never archived and never posted."""
    global last_usage
    last_usage = None
    payload = build_payload(program, tree, inst, stored, escalations)
    data = _cleaned(_generate(client or _default_client(), model, load_prompt(), payload))
    invented = audit_numbers(_narrative_texts(data), payload)
    if invented:
        raise NarrateError(
            "brief rejected — number(s) not in the payload: " + ", ".join(invented)
        )
    return Brief(
        program_id=program.id,
        week_ending=date.fromisoformat(payload["week_ending"]),
        run_id=payload["run_id"],
        payload=payload,
        narrative=data,
        brief=render_brief(payload, data),
        model=model,
        prompt_version=prompt_version(),
    )


# --- CLI -------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Exit codes match the other stages: 0 the brief was written (and posted,
    with --post); 2 it could not be — missing config, nothing tracked, or a
    brief the audit refused. There is no exit 1: a half-brief is not a
    deliverable."""
    from collectors import programs
    from drift.notify import SlackWebhookSender

    ap = argparse.ArgumentParser(
        prog="python -m kpi.narrate",
        description="Write the weekly SVP brief from the stored readings; archive and post it.",
    )
    ap.add_argument("--program", required=True, choices=sorted(programs.PROGRAMS))
    ap.add_argument("--model", default=MODEL)
    ap.add_argument(
        "--post", action="store_true",
        help="post to Slack (settings.slack_webhook_url) and stamp posted_at",
    )
    ap.add_argument(
        "--no-archive", action="store_true",
        help="print only — skip the kpi_briefs archive (development)",
    )
    args = ap.parse_args(argv)

    program = programs.get(args.program)
    dsn = os.environ.get("EVAL_DATABASE_URL")
    if not dsn:
        print(
            "EVAL_DATABASE_URL is not set (it lives in ~/.zshrc — RC1-263); "
            "the readings and the archive both live behind it",
            file=sys.stderr,
        )
        return 2

    try:
        tree = instrument.load_adopted_tree(program.id)
        inst = track.load_instrumentation(program.id)
        with ReadingsStore(dsn) as readings_store:
            stored = readings_store.readings(program.id)
        escalations: list[dict] = []
        if stored:
            week_ending = max(sr.reading.sim_date for sr in stored)
            with EscalationsStore(dsn) as esc_store:
                escalations = [
                    escalation_entry(e)
                    for e in esc_store.escalations(
                        program.id, since=week_ending - timedelta(days=WEEK_DAYS - 1)
                    )
                ]
        brief = narrate(program, tree, inst, stored, escalations=escalations, model=args.model)
    except (NarrateError, FileNotFoundError) as exc:
        print(f"{args.program}: {exc}", file=sys.stderr)
        return 2

    print(brief.brief)
    if last_usage:
        print(
            f"\n[{args.model}: {last_usage.input_tokens} in / "
            f"{last_usage.output_tokens} out]",
            file=sys.stderr,
        )

    if not args.no_archive:
        with BriefsStore(dsn) as briefs_store:
            briefs_store.save(brief)
            print(f"archived: {program.id} week ending {brief.week_ending}", file=sys.stderr)
            if args.post:
                webhook = settings.slack_webhook_url
                if not webhook:
                    print(
                        "SLACK_WEBHOOK_URL is not set; the brief is archived but not posted",
                        file=sys.stderr,
                    )
                    return 2
                SlackWebhookSender(webhook).channel(brief.brief)
                briefs_store.mark_posted(program.id, brief.week_ending)
                print("posted to Slack", file=sys.stderr)
    elif args.post:
        print("--post ignored with --no-archive: an unarchived brief is never posted",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
