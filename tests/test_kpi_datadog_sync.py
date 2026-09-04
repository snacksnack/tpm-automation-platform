"""The exported half of the Datadog account — offline, no network (RC1-378).

Two things are worth a test here and the rest is the API's business:

- **`normalize` drops state and only state.** A leftover `modified_at` makes
  the daily drift job cry wolf every morning until someone stops reading it;
  a dropped `steps` list makes `push` quietly delete a browser test's asserts.
  The fixtures below are trimmed real responses, so the shapes are the
  account's, not invented.
- **The generated objects stay out.** `kpi.datadog` owns the two program
  dashboards, six monitors and seven SLOs. Exporting one of those would give
  it two sources of truth that drift apart in opposite directions — the
  failure RC1-378 exists to prevent — so the guard is asserted from both
  sides: it rejects a generated object and it passes a hand-built one.

The manifest itself is checked against the files on disk, which is what
catches an id added to one and not the other.
"""

from __future__ import annotations

import json

import pytest

from kpi import datadog_sync


def test_normalize_drops_datadog_owned_dashboard_fields():
    doc = {
        "id": "aa2-k8g-ya8",
        "title": "Site Observability — hihelloreid",
        "widgets": [{"id": 344684681638188, "definition": {"type": "slo"}}],
        "author_handle": "hire.reid.collins@gmail.com",
        "author_name": "Reid Collins",
        "created_at": "2026-08-31T18:02:11.395832+00:00",
        "modified_at": "2026-09-01T14:31:02.101000+00:00",
        "url": "/dashboard/aa2-k8g-ya8/site-observability-hihelloreid",
    }

    out = datadog_sync.normalize("dashboards", doc)

    assert set(out) == {"id", "title", "widgets"}
    # Widget ids are chosen once and sent back on every PUT — dropping them
    # would make Datadog reassign and the diff churn on every pull.
    assert out["widgets"][0]["id"] == 344684681638188
    assert doc["url"], "normalize must not mutate its argument"


def test_normalize_drops_monitor_mute_state_but_keeps_thresholds():
    doc = {
        "id": 318833109,
        "name": "Fleet LLM cost per call — price signal",
        "query": "sum(last_4h):... > 0.05",
        "tags": ["rc1:377", "fleet:llm-obs"],
        "options": {"thresholds": {"critical": 0.05, "warning": 0.035}, "silenced": {}},
        "overall_state": "OK",
        "overall_state_modified": "2026-09-04T11:00:00+00:00",
        "creator": {"handle": "hire.reid.collins@gmail.com"},
        "created_at": 1756900000000,
        "modified": "2026-09-03T21:14:00+00:00",
        "org_id": 1234567,
        "deleted": None,
    }

    out = datadog_sync.normalize("monitors", doc)

    assert set(out) == {"id", "name", "query", "tags", "options"}
    assert out["options"] == {"thresholds": {"critical": 0.05, "warning": 0.035}}


def test_normalize_keeps_browser_steps_and_drops_their_assigned_ids():
    """RC1-375's lesson, one layer down: a step's `public_id` is assigned on
    save. Keep it and every pull after an edit reads as drift; drop the whole
    step and `push` deletes the assertion the test exists for."""
    doc = {
        "public_id": "sx6-38z-zxj",
        "type": "browser",
        "monitor_id": 317968584,
        "created_at": "2026-08-31T00:00:00+00:00",
        "steps": [
            {
                "name": "thesis line rendered (React executed)",
                "type": "assertPageContains",
                "params": {"value": "Senior TPM"},
                "public_id": "udg-dxt-ivx",
                "allowFailure": False,
                "timeout": 60,
            }
        ],
    }

    out = datadog_sync.normalize("synthetics", doc)

    assert set(out) == {"public_id", "type", "steps"}
    step = out["steps"][0]
    assert step["params"] == {"value": "Senior TPM"}
    assert "public_id" not in step


def test_dumps_is_stable_and_sorted():
    first = datadog_sync.dumps({"b": 1, "a": {"d": 2, "c": 3}})
    assert first == '{\n  "a": {\n    "c": 3,\n    "d": 2\n  },\n  "b": 1\n}\n'
    assert first == datadog_sync.dumps(json.loads(first))


def test_generated_objects_are_refused_by_tag():
    with pytest.raises(SystemExit, match="generated:kpi-datadog"):
        datadog_sync._refuse_generated(
            "monitors",
            317618028,
            {"name": "Program KPI tripped — eval-run-store", "tags": ["generated:kpi-datadog"]},
        )


def test_generated_dashboards_are_refused_by_title():
    """Dashboards carried no tags when these four were built, so the
    generator's title is the guard on that kind."""
    title = sorted(datadog_sync.generated_dashboard_titles())[0]
    with pytest.raises(SystemExit, match="kpi.datadog"):
        datadog_sync._refuse_generated("dashboards", "qz5-nmb-i9d", {"title": title, "tags": []})


def test_hand_built_objects_pass_the_guard():
    datadog_sync._refuse_generated(
        "dashboards", "bwm-uny-qqs", {"title": "Agent Fleet — LLM Observability", "tags": []}
    )
    datadog_sync._refuse_generated(
        "monitors", 318097614, {"name": "Fleet LLM spend guardrail — daily", "tags": ["rc1:349"]}
    )


def test_every_manifest_entry_has_a_file_and_a_reason():
    for kind, entries in datadog_sync.load_manifest().items():
        assert entries, f"{kind} is listed but empty"
        for entry in entries:
            path = datadog_sync.path_for(kind, str(entry["id"]))
            assert path.exists(), f"{path} is in the manifest but not on disk — run pull"
            assert entry["why"].strip(), f"{kind}/{entry['id']} needs a one-line why"


def test_every_exported_file_is_in_the_manifest():
    """The other direction: a file left behind after an id was dropped would
    never be pulled or pushed again, and would quietly rot."""
    manifest = datadog_sync.load_manifest()
    for kind in datadog_sync.KINDS:
        listed = {str(entry["id"]) for entry in manifest[kind]}
        on_disk = {p.stem for p in (datadog_sync.ROOT / kind).glob("*.json")}
        assert on_disk == listed, f"{kind}: {on_disk ^ listed} is on one side only"


def test_exported_files_carry_no_generated_object():
    """The seed reviewed by hand once; this keeps it true."""
    for kind, entries in datadog_sync.load_manifest().items():
        for entry in entries:
            obj_id = str(entry["id"])
            doc = json.loads(datadog_sync.path_for(kind, obj_id).read_text())
            datadog_sync._refuse_generated(kind, obj_id, doc)


def test_exported_files_are_already_normalized():
    """`pull` writes what `dumps(normalize(...))` produces. If a committed file
    disagrees, it was hand-edited into a shape the next pull will rewrite."""
    for kind, entries in datadog_sync.load_manifest().items():
        for entry in entries:
            path = datadog_sync.path_for(kind, str(entry["id"]))
            doc = json.loads(path.read_text())
            assert path.read_text() == datadog_sync.dumps(datadog_sync.normalize(kind, doc)), (
                f"{path} is not in canonical form — re-run pull, or push your edit"
            )
