"""The checks must fail on bad output (RC1-261).

`python -m evals degrade` asks whether removing a rule from the *prompt* makes
the matching characteristic go red, and on four of five rules it does not. That
result has two possible readings — the model is robust, or the check is asleep —
and it cannot tell them apart, because it only ever sees output the model chose
to produce.

These tests settle it from the other side. Each hands a check a digest that is
wrong in exactly one way and asserts it says so. They are free, deterministic,
and run in CI, which makes them the load-bearing half of the pair: once a check
is known to fail on bad output, a NO SIGNAL from `degrade` is a finding about
the prompt (the rule is redundant with the output schema) rather than a worry
about the check.
"""

from __future__ import annotations

from agent_evals import groundedness

from evals import checks
from evals.corpus import BY_ID, SNAPSHOT
from narrative.drift_digest import build_payload
from narrative.models import DigestLine, DriftDigest

FINDINGS, RESOLVED = BY_ID["mixed-buckets"]


def _digest(lines: list[DigestLine], **kw) -> DriftDigest:
    return DriftDigest(
        subject=kw.get("subject", "RC1 dependency drift: RC1-2 exposed"),
        summary=kw.get("summary", "One red, one yellow, one white."),
        findings=lines,
        all_clear=False,
    )


def _faithful_lines() -> list[DigestLine]:
    glyphs = checks.GLYPHS
    return [
        DigestLine(
            downstream=f.downstream,
            bucket=f.severity_bucket,
            line=f"{glyphs[f.severity_bucket]} {f.downstream} — {f.detail}",
        )
        for f in FINDINGS
    ]


def test_a_faithful_digest_passes_every_gating_check():
    """The baseline. Without this the failure tests prove nothing — a check that
    rejects everything would pass them all."""
    digest = _digest(_faithful_lines())
    payload = build_payload(FINDINGS, SNAPSHOT, RESOLVED)

    assert checks.echoes_every_finding_once(digest, FINDINGS).passed
    assert checks.never_re_ranks_severity(digest, FINDINGS).passed
    assert checks.orders_new_red_first(digest, FINDINGS).passed
    assert checks.marks_buckets_with_the_right_glyph(digest).passed
    assert checks.names_the_project(digest, "RC1").passed
    assert checks.no_unsupported_claims(digest, payload)[0].passed


def test_a_dropped_finding_is_caught():
    result = checks.echoes_every_finding_once(_digest(_faithful_lines()[:-1]), FINDINGS)
    assert not result.passed
    assert "RC1-22" in result.detail, "must name what went missing, not just the count"


def test_a_duplicated_finding_is_caught():
    lines = _faithful_lines()
    result = checks.echoes_every_finding_once(_digest([*lines, lines[0]]), FINDINGS)
    assert not result.passed
    assert "RC1-2" in result.detail


def test_a_softened_severity_is_caught():
    """The failure the whole architecture is arranged to prevent: the model
    taking over a decision the rules engine made."""
    lines = _faithful_lines()
    lines[0] = DigestLine(downstream="RC1-2", bucket="yellow", line="🟡 RC1-2 — some risk")
    result = checks.never_re_ranks_severity(_digest(lines), FINDINGS)
    assert not result.passed
    assert "'red'" in result.detail and "'yellow'" in result.detail


def test_a_reordered_digest_is_caught():
    result = checks.orders_new_red_first(_digest(list(reversed(_faithful_lines()))), FINDINGS)
    assert not result.passed


def test_reordering_within_a_band_is_allowed():
    """The template orders bands, not items inside one. Asserting otherwise
    would invent a requirement and fail correct output."""
    findings, _ = BY_ID["new-red-among-old-red"]
    new_first = [
        DigestLine(downstream="RC1-31", bucket="red", line="🔴 RC1-31"),
        DigestLine(downstream="RC1-15", bucket="red", line="🔴 RC1-15"),
    ]
    assert checks.orders_new_red_first(_digest(new_first), findings).passed
    # ...but a stale red ahead of a new one is the one order the rule forbids.
    assert not checks.orders_new_red_first(_digest(list(reversed(new_first))), findings).passed


def test_a_wrong_glyph_is_caught():
    lines = _faithful_lines()
    lines[0] = DigestLine(downstream="RC1-2", bucket="red", line="🟡 RC1-2 — mislabelled")
    result = checks.marks_buckets_with_the_right_glyph(_digest(lines))
    assert not result.passed
    assert "🔴" in result.detail


def test_an_invented_ticket_key_is_caught():
    lines = _faithful_lines()
    lines[0] = DigestLine(
        downstream="RC1-2", bucket="red", line="🔴 RC1-2 is blocked by RC1-999, slipping 14 days"
    )
    payload = build_payload(FINDINGS, SNAPSHOT, RESOLVED)
    result, report = checks.no_unsupported_claims(_digest(lines), payload)
    assert not result.passed
    assert any(v.kind == "invented_ticket_key" for v in report.violations)


def test_an_invented_day_count_is_caught():
    lines = _faithful_lines()
    lines[0] = DigestLine(downstream="RC1-2", bucket="red", line="🔴 RC1-2 — RC1-1 slipped 45 days")
    payload = build_payload(FINDINGS, SNAPSHOT, RESOLVED)
    result, _ = checks.no_unsupported_claims(_digest(lines), payload)
    assert not result.passed


def test_the_real_day_count_is_not_flagged():
    """The regression that sent a fix upstream (agent-evals v0.1.2): the payload
    writes the slip as `(14d)`, and a digest saying "14 days" was being called a
    hallucination."""
    payload = build_payload(FINDINGS, SNAPSHOT, RESOLVED)
    lines = _faithful_lines()
    lines[0] = DigestLine(
        downstream="RC1-2", bucket="red", line="🔴 RC1-2 — RC1-1 slipped 14 days to 2026-08-06"
    )
    result, _ = checks.no_unsupported_claims(_digest(lines), payload)
    assert result.passed, "a day count spelled out from the facts is not invented"


def test_a_subject_without_the_project_is_caught():
    digest = _digest(_faithful_lines(), subject="Dependency drift: one item exposed")
    assert not checks.names_the_project(digest, "RC1").passed


def test_the_all_clear_summary_is_not_read_as_inventing_drift():
    """"No dependency drift detected" contains "drift detected".

    The first run of the free subject failed on exactly this, and it is the
    reason the check is negation-aware rather than a substring scan.
    """
    said = groundedness.must_not_say(
        "No dependency drift detected in RC1.", ["drift detected"], negation_aware=True
    )
    assert not said


# --- RC1-255: the template contract, free and gating ----------------------


def test_every_rule_the_checks_depend_on_is_still_in_the_template():
    """Degrading the prompt must fail CI, and before this it did not.

    The gating characteristics are scored by the billed subject, which by design
    never runs in CI (ADR-0031). So removing the glyph rule — which
    `evals degrade` measured as genuinely load-bearing — passed ruff, passed
    pytest, and passed the free eval. The regression was real and invisible.

    `evals/degrade.py` already declares, for each characteristic, the exact
    template text it depends on. That catalogue is the contract: this test
    asserts every entry is still present verbatim. It costs nothing, runs on
    every push, and names the rule that moved rather than reporting a vague
    failure.
    """
    from evals import degrade

    missing = []
    for degradation in degrade.DEGRADATIONS:
        try:
            degrade.degraded_prompt(degradation)
        except ValueError:
            missing.append(f"{degradation.name} (protects {degradation.expect_breaks})")
    assert not missing, (
        "the drift-digest template no longer contains: "
        + "; ".join(missing)
        + ". Either restore the rule, or update evals/degrade.py — but a rule "
        "removed without updating the catalogue means the eval silently stops "
        "testing the characteristic it protects."
    )


def test_the_glyphs_the_checker_requires_are_the_glyphs_the_template_mandates():
    """`checks.GLYPHS` and the template have to agree.

    The checker asserts a red line opens with 🔴. If the template were edited to
    ask for a different marker, every red line would fail a check that looks
    like it is about the model — when it is really about two files disagreeing.
    """
    from narrative import drift_digest

    template = drift_digest.load_prompt()
    for bucket, glyph in checks.GLYPHS.items():
        assert glyph in template, (
            f"checks.GLYPHS maps {bucket!r} to {glyph}, which the template no longer mentions"
        )
