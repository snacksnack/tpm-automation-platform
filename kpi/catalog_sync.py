"""The Software Catalog, as files (RC1-447).

`kpi.datadog_sync` is account-first: the objects it manages were built in the
UI, and the files are an export that a daily `diff` keeps honest. Catalog
entities are the other way round. Nothing in Datadog created them — every one
is authored here, in `datadog/entities/*.yaml`, and pushed up. The file is the
source, not a backup of one, which is why this is a separate module rather
than a fifth `KINDS` entry. (`kpi.catalog` is taken — that one is the
program source catalog from RC1-303, nothing to do with Datadog.)

    python -m kpi.catalog_sync push    # datadog/entities/*.yaml -> account
    python -m kpi.catalog_sync diff    # exit 1 if the account has drifted

`push` is an upsert keyed on `metadata.name`; the API answers 202 and applies
asynchronously, so `diff` immediately after a `push` can still read the old
entity for a beat.

What the eight entities record, beyond owner and tier, is the estate's naming
problem. A catalog entity is keyed on exactly one name, but every deployed
service here answers to as many as three: `tpm-drift-detector` to DORA,
`drift-service` to APM, `drift-digest` to LLM Observability, and not one of
the four DORA service names appears in APM at all. The entity is keyed on the
DORA name, because that is the deploy identity, and the others are carried as
`apm-service:` and `ml-app:` tags so the split is declared rather than
discovered. Converging them is its own ticket: a Datadog service name is an
identity, so changing `DD_SERVICE` starts a new service and leaves the history
behind under the old one.

Two tags mark real gaps rather than names: `gap:no-deploy-events` on the n8n
workflow, which is re-imported by hand so nothing observes its deploy, and
`gap:dd-service-unset` on the summarizer, whose other seven Lambdas report
under raw CloudFormation names that carry a stack hash and change when the
stack is recreated.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx
import yaml

from kpi.datadog_sync import client

#: Authored entity definitions. One file per service, named for the entity.
ENTITIES = Path(__file__).resolve().parent.parent / "datadog" / "entities"

#: Fields the account adds on its own. They are not authored, so comparing
#: them would report drift every time the API touched an entity.
STRIP = ("id", "namespace", "managed", "createdAt", "modifiedAt", "relationships")


def load() -> dict[str, dict]:
    """Every authored entity, keyed on `metadata.name`."""
    out: dict[str, dict] = {}
    for path in sorted(ENTITIES.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text())
        name = doc["metadata"]["name"]
        if name != path.stem:
            raise SystemExit(
                f"{path.name} declares metadata.name {name!r}; the file must be "
                "named for the entity so a reader can find it by name"
            )
        if name in out:
            raise SystemExit(f"two files declare the entity {name!r}")
        out[name] = doc
    if not out:
        raise SystemExit(f"no entity files found under {ENTITIES}")
    return out


def normalize(doc: dict) -> dict:
    """An entity reduced to what this repo authors, for comparison."""
    out = {k: v for k, v in doc.items() if k not in STRIP}
    meta = {k: v for k, v in out.get("metadata", {}).items() if k not in STRIP}
    if meta.get("tags"):
        meta["tags"] = sorted(meta["tags"])
    out["metadata"] = meta
    return out


def fetch(http: httpx.Client, name: str) -> dict | None:
    """The account's copy of one entity's schema, or None if it has none."""
    resp = http.get(
        "/api/v2/catalog/entity",
        params={"filter[name]": name, "include": "schema", "page[limit]": 1},
    )
    resp.raise_for_status()
    for item in resp.json().get("included", []):
        if item.get("type") == "schema":
            return item.get("attributes", {}).get("schema")
    return None


def push() -> list[str]:
    """Upsert every authored entity. Returns the names sent."""
    docs = load()
    with client() as http:
        for name, doc in docs.items():
            resp = http.post("/api/v2/catalog/entity", json=doc)
            if resp.status_code >= 300:
                raise SystemExit(f"{name}: HTTP {resp.status_code} {resp.text[:300]}")
    return sorted(docs)


def diff() -> tuple[list[str], bool]:
    """Report entities the account disagrees with. Returns (lines, drifted)."""
    docs = load()
    lines: list[str] = []
    with client() as http:
        for name, doc in docs.items():
            live = fetch(http, name)
            if live is None:
                lines.append(f"{name}: absent from the account — run `push`")
                continue
            want, got = normalize(doc), normalize(live)
            if want != got:
                lines.append(f"{name}: differs from datadog/entities/{name}.yaml")
                lines.append(f"  file:    {json.dumps(want, sort_keys=True)}")
                lines.append(f"  account: {json.dumps(got, sort_keys=True)}")
    return lines, bool(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m kpi.catalog_sync",
        description="Software Catalog entities, authored in datadog/entities/.",
    )
    ap.add_argument("cmd", choices=["push", "diff"])
    cmd = ap.parse_args(argv).cmd

    if cmd == "push":
        names = push()
        print(f"pushed — {len(names)} entities: {', '.join(names)}")
        return 0

    lines, drifted = diff()
    if drifted:
        print("\n".join(lines))
        return 1
    print(f"clean — {len(load())} entities match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
