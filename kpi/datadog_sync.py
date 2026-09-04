"""The hand-built half of the Datadog account, as files (RC1-378).

`kpi.datadog` *generates* the two program dashboards, the six KPI monitors and
the seven program SLOs from the adopted trees — those have a source, and
regenerating them is one command. Everything else in the account was made by
API calls in session scratchpads that are gone: the four dashboards, ten
monitors, five synthetics tests and two availability SLOs listed in
`datadog/manifest.json`. An accidental delete or a bad edit had no undo beyond
Datadog's own version history, no review, and no way to rebuild the account.

This module closes that with exported JSON rather than Terraform. For one
person and ~20 objects, JSON in the repo buys review, backup and reproducibility
with tools the repo already has; Terraform would add state, provider
credentials in CI and an import step, and this export is its first step anyway
if the estate grows. `docs/datadog-as-code.md` carries that argument in full.

    python -m kpi.datadog_sync pull    # account -> datadog/**.json
    python -m kpi.datadog_sync diff    # exit 1 if the account has drifted
    python -m kpi.datadog_sync push    # datadog/**.json -> account

Two rules hold the whole thing up:

- **Never export a generated object.** Two sources of truth for one dashboard
  is the exact failure this module exists to prevent, so `_refuse_generated`
  rejects any id whose object carries `generated:kpi-datadog` or whose title
  is one the generator owns — on pull, on push and on diff alike.
- **Configuration only, never state.** `modified_at`, `overall_state`,
  `creator` and friends change on their own and would make `diff` cry drift
  every day; `STRIP` lists them per kind. The RC1-375 lesson — a browser
  test's per-step `public_id` is assigned by Datadog, not chosen — is the
  reason that one is nested rather than top-level.

The ids in these files are this account's. A fresh account would need creates,
which is deliberately out of scope: this is a backup and a review surface, not
a provisioner.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from pathlib import Path

import httpx

from kpi.datadog import api_url

#: Repo-root `datadog/`, beside `kpi/` rather than inside it — the files are
#: the account's, not the KPI program's.
ROOT = Path(__file__).resolve().parent.parent / "datadog"
MANIFEST = ROOT / "manifest.json"

GENERATED_TAG = "generated:kpi-datadog"

#: Fields Datadog owns. Stripped on the way in so `diff` speaks only about
#: configuration; absent on the way out so a push never tries to set them.
#: Tuples are nested paths; `("steps", "*", "public_id")` means "that key in
#: every element of the list at `steps`".
STRIP: dict[str, list[str | tuple[str, ...]]] = {
    "dashboards": ["author_handle", "author_name", "created_at", "modified_at", "url"],
    "monitors": [
        "created",
        "created_at",
        "creator",
        "deleted",
        "matching_downtimes",
        "modified",
        "org_id",
        "overall_state",
        "overall_state_modified",
        # Mute state, not configuration: a downtime would otherwise read as drift.
        ("options", "silenced"),
    ],
    "synthetics": [
        "created_at",
        "modified_at",
        "creator",
        "org_id",
        # Datadog creates the alert monitor for a test and assigns its id; the
        # test is the source, so the monitor file is a read-only copy (RC1-378).
        "monitor_id",
        # RC1-375: per-step ids are assigned on save, not chosen.
        ("steps", "*", "public_id"),
    ],
    "slos": ["created_at", "modified_at", "creator"],
}

#: kind -> (GET path, PUT path). `{id}` is the object's id; both synthetics
#: paths need the test's own `type`, so reading one takes the detour in
#: `_get_synthetic` and writing one takes the type from the document.
PATHS = {
    "dashboards": ("/api/v1/dashboard/{id}", "/api/v1/dashboard/{id}"),
    "monitors": ("/api/v1/monitor/{id}", "/api/v1/monitor/{id}"),
    "synthetics": (None, "/api/v1/synthetics/tests/{type}/{id}"),
    "slos": ("/api/v1/slo/{id}", "/api/v1/slo/{id}"),
}

KINDS = tuple(PATHS)


def _prune(node: dict, path: tuple[str, ...]) -> None:
    """Drop `path` from `node` in place. A `*` segment fans out over a list."""
    head, *rest = path
    if not rest:
        node.pop(head, None)
        return
    child = node.get(head)
    if child is None:
        return
    if rest[0] == "*":
        for item in child:
            if isinstance(item, dict):
                _prune(item, tuple(rest[1:]))
    elif isinstance(child, dict):
        _prune(child, tuple(rest))


def normalize(kind: str, doc: dict) -> dict:
    """The account's object as it should sit in the repo: state stripped, keys
    sorted by `dumps`. Pure — the caller's dict is left alone."""
    out = json.loads(json.dumps(doc))
    for path in STRIP[kind]:
        _prune(out, (path,) if isinstance(path, str) else path)
    return out


def dumps(doc: dict) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def path_for(kind: str, obj_id: str) -> Path:
    return ROOT / kind / f"{obj_id}.json"


def load_manifest() -> dict[str, list[dict]]:
    manifest = json.loads(MANIFEST.read_text())
    return {kind: manifest.get(kind, []) for kind in KINDS}


def generated_dashboard_titles() -> set[str]:
    """The generator tags its monitors and SLOs, so the tag catches those. It
    cannot tag a dashboard — Datadog's dashboard API accepts only `team:` and
    `ai:` tag keys and 400s on anything else — so the generator's own title is
    the guard on that kind. `kpi.datadog.dashboard_payload` builds the same
    string; changing it there means changing it here."""
    from collectors import programs

    return {f"Program KPIs — {program_id}" for program_id in programs.PROGRAMS}


def _refuse_generated(kind: str, obj_id: str, doc: dict) -> None:
    if GENERATED_TAG in (doc.get("tags") or []):
        raise SystemExit(
            f"{kind}/{obj_id} carries {GENERATED_TAG}: it is generated by "
            "kpi.datadog and must not be exported. Two sources of truth for "
            "one object is the failure RC1-378 exists to prevent — drop it "
            "from datadog/manifest.json."
        )
    if kind == "dashboards" and doc.get("title") in generated_dashboard_titles():
        raise SystemExit(
            f"dashboards/{obj_id} ({doc['title']!r}) is generated by "
            "`python -m kpi.datadog dashboards --push` — drop it from "
            "datadog/manifest.json."
        )


def client() -> httpx.Client:
    api_key = os.environ.get("DD_API_KEY")
    app_key = os.environ.get("DD_APP_KEY")
    if not api_key or not app_key:
        raise SystemExit(
            "DD_API_KEY and DD_APP_KEY are required to reach Datadog "
            "(they live in ~/.zshrc; CI takes them from repo secrets)"
        )
    return httpx.Client(
        base_url=api_url(""),
        headers={"DD-API-KEY": api_key, "DD-APPLICATION-KEY": app_key},
        timeout=30,
    )


def _get_synthetic(http: httpx.Client, public_id: str) -> dict:
    """A synthetics test, whole.

    The untyped `/synthetics/tests/{public_id}` endpoint answers for every
    kind but silently omits a browser test's `steps` — and the steps *are* the
    browser test. Read it once for the `type`, then read the typed endpoint
    that carries everything. Two calls over five tests, and no `type` field in
    the manifest to fall out of date.
    """
    resp = http.get(f"/api/v1/synthetics/tests/{public_id}")
    resp.raise_for_status()
    typed = http.get(f"/api/v1/synthetics/tests/{resp.json()['type']}/{public_id}")
    typed.raise_for_status()
    return typed.json()


def fetch(http: httpx.Client, kind: str, obj_id: str) -> dict:
    """One object from the account, normalized. Raises if it is generated."""
    if kind == "synthetics":
        doc = _get_synthetic(http, obj_id)
    else:
        resp = http.get(PATHS[kind][0].format(id=obj_id))
        resp.raise_for_status()
        body = resp.json()
        # The SLO endpoint wraps its object; the other two do not.
        doc = body["data"] if kind == "slos" else body
    _refuse_generated(kind, obj_id, doc)
    return normalize(kind, doc)


#: Keys the file keeps but a write endpoint refuses. The browser-test PUT
#: rejects `public_id` outright ("Additional properties are not allowed") —
#: the id is already in the path — while the API-test PUT tolerates it; the
#: files keep it either way so each one names the object it came from.
WRITE_STRIP = {"synthetics": ["public_id"]}


def put(http: httpx.Client, kind: str, obj_id: str, doc: dict) -> httpx.Response:
    path = PATHS[kind][1].format(id=obj_id, type=doc.get("type", ""))
    body = {k: v for k, v in doc.items() if k not in WRITE_STRIP.get(kind, ())}
    return http.put(path, json=body)


def pull() -> list[str]:
    """Every manifest object into its file. Returns one line per object."""
    lines = []
    with client() as http:
        for kind, entries in load_manifest().items():
            for entry in entries:
                obj_id = str(entry["id"])
                doc = fetch(http, kind, obj_id)
                dest = path_for(kind, obj_id)
                dest.parent.mkdir(parents=True, exist_ok=True)
                before = dest.read_text() if dest.exists() else None
                text = dumps(doc)
                dest.write_text(text)
                verb = "new" if before is None else ("updated" if before != text else "same")
                lines.append(f"{verb:>7}  {kind}/{obj_id}  {entry['why']}")
    return lines


def diff() -> tuple[list[str], bool]:
    """Compare the account with the files. Returns (lines, drifted)."""
    lines: list[str] = []
    drifted = False
    with client() as http:
        for kind, entries in load_manifest().items():
            for entry in entries:
                obj_id = str(entry["id"])
                dest = path_for(kind, obj_id)
                live = dumps(fetch(http, kind, obj_id))
                if not dest.exists():
                    drifted = True
                    lines.append(f"missing  {dest.relative_to(ROOT.parent)} — run pull")
                    continue
                stored = dest.read_text()
                if stored == live:
                    continue
                drifted = True
                lines.extend(
                    difflib.unified_diff(
                        stored.splitlines(),
                        live.splitlines(),
                        fromfile=f"repo/{kind}/{obj_id}.json",
                        tofile=f"datadog/{kind}/{obj_id}.json",
                        lineterm="",
                    )
                )
    return lines, drifted


def push() -> list[str]:
    """Every pushable manifest object from its file into the account.

    `push: false` entries are skipped: a synthetics test owns its alert
    monitor, so that monitor's file is a backup and a review surface, and the
    test file next to it is what a change goes through.
    """
    lines = []
    with client() as http:
        for kind, entries in load_manifest().items():
            for entry in entries:
                obj_id = str(entry["id"])
                if entry.get("push") is False:
                    lines.append(f"skipped  {kind}/{obj_id}  {entry.get('push_note', '')}")
                    continue
                doc = json.loads(path_for(kind, obj_id).read_text())
                _refuse_generated(kind, obj_id, doc)
                resp = put(http, kind, obj_id, doc)
                if resp.is_error:
                    raise SystemExit(
                        f"push {kind}/{obj_id} failed {resp.status_code}: {resp.text[:400]}"
                    )
                lines.append(f"pushed  {kind}/{obj_id}")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m kpi.datadog_sync",
        description=(
            "The hand-built Datadog objects as files: pull them down, push them "
            "back, or fail on drift (RC1-378). The generated objects belong to "
            "`python -m kpi.datadog` and are refused here."
        ),
    )
    ap.add_argument("cmd", choices=["pull", "push", "diff"])
    args = ap.parse_args(argv)

    if args.cmd == "pull":
        for line in pull():
            print(line)
        return 0
    if args.cmd == "push":
        for line in push():
            print(line)
        return 0

    lines, drifted = diff()
    for line in lines:
        print(line)
    if drifted:
        print(
            "\nThe account and the repo disagree. Someone edited in the UI: "
            "`pull` and commit to keep the edit, or `push` to undo it.",
            file=sys.stderr,
        )
        return 1
    print(f"clean — {sum(len(v) for v in load_manifest().values())} objects match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
