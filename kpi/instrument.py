"""Instrument stage: verify every KPI in a tree against real snapshots (RC1-303).

A KPI without a verified source does not ship. The define stage named
sources from a brief; this stage checks each KPI against what the collector
actually stores (the source catalog, `kpi.catalog`) and against a computation
that actually runs (`kpi.measures`). Same split as define: the model proposes,
the code enforces.

The model reads the adopted tree, the rubric and the catalog — including the
latest snapshot's per-source health — and returns a verdict per KPI:
`confirmed` with the fields and query, `proxied` with the stand-in and what
it misses, or `rejected` with the missing source named. The code then:

1. refuses any cited field that is not in the catalog (test 4's "measurable
   in principle does not count", made mechanical);
2. requires a proxy to say what it misses and a rejection to say why;
3. runs the registered measure for every confirmed or proxied KPI against
   the stored snapshot series — a verdict of "confirmed" with nothing that
   computes it is downgraded to `unverified`;
4. runs the same measure against the latest snapshot with every source
   removed, and requires a `broken` or `stale` reading with a reason. That is
   the planted source break, checked per KPI rather than promised.

The report (`docs/kpi/instruments/<program>.md`, with a JSON twin) is what
the track stage (RC1-305) reads: it tracks the verified set, carries each
proxy's caveat into every brief, and does not touch the rejected.

Usage:
    python -m kpi.instrument --program simulated-program \\
        --out docs/kpi/instruments/simulated-program.md          # billed, one call
    python -m kpi.instrument --program eval-run-store --db data/drift.db --out ...

Needs ANTHROPIC_API_KEY (config reads .env) and a store with at least one
snapshot of the program (`python -m collectors snapshot <program>`).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from collectors.models import ProgramSnapshot
from collectors.programs import Program
from kpi import RUBRIC_VERSION, catalog, measures
from kpi.define import MODEL, RUBRIC, DefineError, prompt_version, rubric_version_declared
from kpi.models import KpiTree
from kpi.reading import Reading
from observability import enable_llm_obs

_TEMPLATE = Path(__file__).parent / "templates" / "instrument.md"
TREES = Path(__file__).resolve().parent.parent / "docs" / "kpi" / "trees"
#: Where this stage's reports land, and where the track stage (RC1-305) reads
#: the verified set back from — `<program>.json` beside the markdown.
INSTRUMENTS = Path(__file__).resolve().parent.parent / "docs" / "kpi" / "instruments"

Verdict = Literal["confirmed", "proxied", "rejected"]
Status = Literal["verified", "unverified", "rejected"]


class InstrumentError(RuntimeError):
    pass


def load_prompt() -> str:
    return _TEMPLATE.read_text()


def load_adopted_tree(program_id: str) -> KpiTree:
    path = TREES / f"{program_id}.adopted.json"
    if not path.exists():
        raise InstrumentError(f"no adopted tree at {path}")
    return KpiTree.model_validate_json(path.read_text())


# --- the model's verdicts ----------------------------------------------------------------


class KpiVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kpi_id: str
    verdict: Verdict
    fields: list[str] = Field(default_factory=list, description="dotted catalog names used")
    query: str = Field(default="", description="how the value is computed from `fields`")
    proxy: str | None = None
    misses: str | None = None
    reason: str = ""
    caveat: str | None = Field(
        default=None, description="what today's reading will be, if a source is down"
    )


class Verdicts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    program: str
    verdicts: list[KpiVerdict]
    notes: list[str] = Field(default_factory=list)


_VERDICT = {
    "type": "object",
    "properties": {
        "kpi_id": {"type": "string"},
        "verdict": {"type": "string", "enum": ["confirmed", "proxied", "rejected"]},
        "fields": {"type": "array", "items": {"type": "string"}},
        "query": {"type": "string"},
        "proxy": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "misses": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "reason": {"type": "string"},
        "caveat": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["kpi_id", "verdict", "fields", "query", "proxy", "misses", "reason", "caveat"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "program": {"type": "string"},
        "verdicts": {"type": "array", "items": _VERDICT},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["program", "verdicts", "notes"],
    "additionalProperties": False,
}


def build_payload(tree: KpiTree, rubric: str, source_catalog: dict) -> dict:
    """Exactly what the model sees: the tree, the rubric, the catalog."""
    return {
        "rubric": rubric,
        "kpi_tree": tree.model_dump(mode="json"),
        "source_catalog": source_catalog,
    }


@dataclass(frozen=True)
class CallUsage:
    input_tokens: int
    output_tokens: int


last_usage: CallUsage | None = None


def _default_client():
    import anthropic

    from config import settings

    return anthropic.Anthropic(
        api_key=settings.anthropic_api_key or None, timeout=180.0, max_retries=3
    )


def propose(
    tree: KpiTree,
    source_catalog: dict,
    *,
    rubric: str | None = None,
    client=None,
    model: str = MODEL,
) -> Verdicts:
    """One model call: the verdicts, unverified. `verify` is what makes them count."""
    global last_usage
    last_usage = None
    rubric_text = rubric if rubric is not None else RUBRIC.read_text()
    declared = rubric_version_declared(rubric_text)
    if declared != RUBRIC_VERSION:
        raise DefineError(
            f"rubric.md declares v{declared} but kpi.RUBRIC_VERSION is {RUBRIC_VERSION}; "
            "bump both together"
        )
    resp = (client or _default_client()).messages.create(
        model=model,
        max_tokens=8192,
        system=load_prompt(),
        messages=[
            {
                "role": "user",
                "content": json.dumps(build_payload(tree, rubric_text, source_catalog)),
            }
        ],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
    )
    usage = getattr(resp, "usage", None)
    last_usage = CallUsage(
        getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0
    )
    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
    if text is None:
        raise InstrumentError("model returned no text block")
    return Verdicts.model_validate_json(text)


# --- what the code enforces ---------------------------------------------------------------


class KpiInstrument(BaseModel):
    """One KPI, instrumented: the model's verdict and what the code found."""

    model_config = ConfigDict(extra="forbid")

    kpi_id: str
    verdict: Verdict
    status: Status
    fields: list[str]
    query: str
    proxy: str | None = None
    misses: str | None = None
    reason: str = ""
    caveat: str | None = None
    measure: str | None = Field(default=None, description="registered measure name, if any")
    sample: Reading | None = Field(default=None, description="the measure on the latest snapshot")
    when_missing: Reading | None = Field(
        default=None, description="the measure with every source removed"
    )
    problems: list[str] = Field(default_factory=list)


class Instrumentation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    program: str
    rubric_version: int
    prompt_version: int | None
    model: str | None
    instrumented_at: datetime
    sample_sim_date: str | None
    kpis: list[KpiInstrument]
    notes: list[str] = Field(default_factory=list)

    def by_status(self, status: Status) -> list[KpiInstrument]:
        return [k for k in self.kpis if k.status == status]

    @property
    def computes(self) -> list[str]:
        """The confirmed set that computes from snapshots without manual steps."""
        return [k.kpi_id for k in self.kpis if k.status == "verified"]


def verify(
    verdicts: Verdicts,
    tree: KpiTree,
    program: Program,
    series: list[ProgramSnapshot],
    *,
    model: str | None = None,
    now: datetime | None = None,
) -> Instrumentation:
    """Turn the model's verdicts into an instrumentation the code stands behind."""
    available = catalog.available_fields(program)
    by_id = {v.kpi_id: v for v in verdicts.verdicts}
    out: list[KpiInstrument] = []
    for kpi in tree.kpis:
        v = by_id.get(kpi.id)
        if v is None:
            out.append(
                KpiInstrument(
                    kpi_id=kpi.id, verdict="rejected", status="unverified", fields=[], query="",
                    reason="the model returned no verdict for this KPI",
                    problems=["no verdict"],
                )
            )
            continue
        problems: list[str] = []
        unknown = sorted(f for f in v.fields if f not in available)
        if unknown:
            problems.append(f"cites field(s) not in the catalog: {', '.join(unknown)}")
        if v.verdict == "proxied" and not (v.proxy and v.misses):
            problems.append("a proxy must define the stand-in and say what it misses")
        if v.verdict == "rejected" and not v.reason.strip():
            problems.append("a rejection must name the missing source")
        if v.verdict in ("confirmed", "proxied") and not v.fields:
            problems.append("confirmed or proxied with no fields cited")

        measure_name = None
        sample = when_missing = None
        if v.verdict in ("confirmed", "proxied"):
            fn = measures.MEASURES.get(kpi.id)
            if fn is None:
                problems.append("no measure is registered: nothing computes this KPI")
            elif not series:
                problems.append("no snapshot of the program in the store to compute against")
            else:
                measure_name = getattr(fn, "__name__", kpi.id)
                try:
                    sample = fn(program, series)
                except Exception as exc:  # a measure that raises is an unverified KPI
                    problems.append(
                        f"measure raised on the sample: {type(exc).__name__}: {exc}"
                    )
                try:
                    when_missing = fn(
                        program, series[:-1] + [measures.source_missing(series[-1])]
                    )
                except Exception as exc:
                    problems.append(
                        f"measure raised with the source removed: {type(exc).__name__}: {exc}"
                    )
                else:
                    # A carried value under `broken` is correct (the ledger carries
                    # the last good reading with its date); a fresh `ok` number is not.
                    if when_missing.state == "ok":
                        problems.append(
                            f"with every source removed the measure still read "
                            f"{when_missing.value!r} as ok; it must read broken or stale "
                            "with a reason"
                        )
        status: Status = (
            "rejected" if v.verdict == "rejected" and not problems
            else "verified" if not problems
            else "unverified"
        )
        out.append(
            KpiInstrument(
                kpi_id=kpi.id, verdict=v.verdict, status=status, fields=v.fields, query=v.query,
                proxy=v.proxy, misses=v.misses, reason=v.reason, caveat=v.caveat,
                measure=measure_name, sample=sample, when_missing=when_missing, problems=problems,
            )
        )
    extra = sorted(set(by_id) - {k.id for k in tree.kpis})
    notes = list(verdicts.notes)
    if extra:
        notes.append(
            f"the model returned verdicts for ids not in the tree, ignored: {', '.join(extra)}"
        )
    return Instrumentation(
        program=program.id,
        rubric_version=RUBRIC_VERSION,
        prompt_version=prompt_version(load_prompt()),
        model=model,
        instrumented_at=now or datetime.now().astimezone(),
        sample_sim_date=series[-1].sim_date.isoformat() if series else None,
        kpis=out,
        notes=notes,
    )


# --- the report ---------------------------------------------------------------------------


def _reading(r: Reading | None) -> str:
    if r is None:
        return "—"
    shown = "no value" if r.value is None else f"{r.value:g}"
    tail = f" [{r.state}: {r.reason}]" if r.state != "ok" else ""
    return f"{shown}{' tripped' if r.tripped else ''}{tail}"


def render_markdown(inst: Instrumentation, tree: KpiTree) -> str:
    names = {k.id: k.name for k in tree.kpis}
    head = [
        f"# Instrumentation — {tree.program}",
        "",
        f"Verified against the snapshot store on {inst.instrumented_at:%Y-%m-%d}"
        + (
            f" (sample: sim-date {inst.sample_sim_date})"
            if inst.sample_sim_date
            else " (no sample)"
        )
        + f", rubric v{inst.rubric_version}, instrument prompt v{inst.prompt_version}"
        + (f", model `{inst.model}`" if inst.model else "")
        + ". Generated by `python -m kpi.instrument`; the verdicts are the model's, "
        "the status is the code's.",
        "",
        "| KPI | verdict | status | measure | sample reading | with sources removed |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for k in inst.kpis:
        head.append(
            f"| `{k.kpi_id}` | {k.verdict} | **{k.status}** | {k.measure or '—'} | "
            f"{_reading(k.sample)} | {_reading(k.when_missing)} |"
        )
    body: list[str] = ["", "## Per KPI", ""]
    for k in inst.kpis:
        body += [f"### `{k.kpi_id}` · {names.get(k.kpi_id, '')}", ""]
        body.append(f"**{k.verdict}**, {k.status}." + (f" {k.reason}" if k.reason else ""))
        body.append("")
        if k.fields:
            body.append(f"- fields: {', '.join(f'`{f}`' for f in k.fields)}")
        if k.query:
            body.append(f"- query: {k.query}")
        if k.proxy:
            body.append(f"- **proxy:** {k.proxy}")
            body.append(f"- **misses:** {k.misses}")
        if k.caveat:
            body.append(f"- caveat: {k.caveat}")
        if k.sample is not None:
            body.append(f"- sample reading: {_reading(k.sample)} — {k.sample.detail}")
        if k.when_missing is not None:
            body.append(f"- with every source removed: {_reading(k.when_missing)}")
        for p in k.problems:
            body.append(f"- ⚠ {p}")
        body.append("")
    verified = inst.computes
    body += [
        "## The confirmed set",
        "",
        f"{len(verified)} of {len(inst.kpis)} compute from snapshots without manual steps: "
        + (", ".join(f"`{k}`" for k in verified) if verified else "none")
        + ".",
    ]
    proxied = [k for k in inst.kpis if k.verdict == "proxied" and k.status == "verified"]
    if proxied:
        body += ["", "Proxies, whose caveat travels with the number into every brief:", ""]
        body += [f"- `{k.kpi_id}` — {k.misses}" for k in proxied]
    rejected = inst.by_status("rejected")
    if rejected:
        body += ["", "Rejected, not tracked:", ""]
        body += [f"- `{k.kpi_id}` — {k.reason}" for k in rejected]
    unverified = inst.by_status("unverified")
    if unverified:
        body += ["", "Unverified — the model's verdict did not survive the code's checks:", ""]
        body += [f"- `{k.kpi_id}` — {'; '.join(k.problems)}" for k in unverified]
    if inst.notes:
        body += ["", "## Notes", ""] + [f"- {n}" for n in inst.notes]
    return "\n".join(head + body) + "\n"


def write_instrumentation(inst: Instrumentation, tree: KpiTree, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(inst, tree))
    twin = out.with_suffix(".json")
    twin.write_text(inst.model_dump_json(indent=2) + "\n")
    return twin


# --- the CLI ---------------------------------------------------------------------------------


def load_series(db_path: str, program_id: str) -> list[ProgramSnapshot]:
    from store.snapshot_store import SnapshotStore

    with SnapshotStore(db_path) as store:
        return [store.load_program_snapshot(r.run_id) for r in store.program_runs(program_id)]


def main(argv: list[str] | None = None) -> int:
    from collectors import programs
    from config import settings

    ap = argparse.ArgumentParser(
        description="Verify a program's adopted KPI tree against its snapshots."
    )
    ap.add_argument("--program", required=True, choices=sorted(programs.PROGRAMS))
    ap.add_argument("--out", required=True, type=Path, help="where to write the report (markdown)")
    ap.add_argument("--db", default=settings.db_path, help="snapshot store (default: DB_PATH)")
    ap.add_argument("--model", default=MODEL)
    args = ap.parse_args(argv)

    if not settings.anthropic_api_key:
        print("ANTHROPIC_API_KEY is not set (config reads .env).", file=sys.stderr)
        return 2
    enable_llm_obs("kpi-agent", service="kpi.instrument")
    program = programs.get(args.program)
    tree = load_adopted_tree(program.id)
    series = load_series(args.db, program.id)
    if not series:
        print(
            f"no snapshot of {program.id!r} in {args.db} — run "
            f"`python -m collectors snapshot {program.id}` first",
            file=sys.stderr,
        )
        return 2
    source_catalog = catalog.catalog(program, series[-1])
    verdicts = propose(tree, source_catalog, model=args.model)
    inst = verify(verdicts, tree, program, series, model=args.model)
    twin = write_instrumentation(inst, tree, args.out)
    u = last_usage
    spend = f" ({u.input_tokens} in / {u.output_tokens} out tokens)" if u else ""
    counts = {s: len(inst.by_status(s)) for s in ("verified", "unverified", "rejected")}
    print(
        f"wrote {args.out} and {twin.name}: {counts['verified']} verified, "
        f"{counts['rejected']} rejected, {counts['unverified']} unverified{spend}"
    )
    return 0 if not counts["unverified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
