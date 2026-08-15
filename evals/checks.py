"""Scoring the drift digest against the promises its own prompt makes (RC1-261).

The prompt template is unusually good raw material for an eval, because almost
every rule in it is stated as an absolute the output either satisfies or does
not: never invent, never re-rank, one line per finding, this order, that glyph.
Nothing here needs a judge — the whole gating set is exact, free, and runs on
every push.

That is worth saying plainly, because the reflex when evaluating an LLM feature
is to reach for an LLM judge. On this subject a judge would be *less* accurate
than a regex on all six gating characteristics, and would cost money to be so.

## What is deliberately not checked here

The summary's bucket counts. The template asks for "the count by bucket" and a
model may reasonably write "two red" or "2 red" or "a pair of red items". A
check strict enough to catch a wrong count flags a right one phrased unusually,
and precision is what keeps a check trusted — so the count lands in the
observations for a human to read, and the characteristic stays advisory.

## Severity is checked here, not by the shared library

`agent_evals.groundedness` has a health-contradiction check, and it does not
apply: it looks for one `health` state asserted about the plan as a whole. Drift
severity is per-finding and echoed structurally in `DigestLine.bucket`, which
makes it exactly checkable rather than a matter of phrasing. Different shape,
different check, and it belongs in the repo that owns the shape.
"""

from __future__ import annotations

from agent_evals import groundedness
from agent_evals.record import CharacteristicResult

from narrative.models import DriftDigest
from store.models import Finding

#: The glyph the template mandates per bucket.
GLYPHS = {"red": "🔴", "yellow": "🟡", "white": "⚪"}

#: The ordering the template mandates, as a sort key. New reds first, then the
#: remaining reds, then yellow, then white.
_RANK = {("red", True): 0, ("red", False): 1, ("yellow", True): 2, ("yellow", False): 2}


def _rank(bucket: str, is_new: bool) -> int:
    return _RANK.get((bucket, is_new), 3)


def _is_new(f: Finding) -> bool:
    """Mirrors `drift_digest._is_new` — an unpersisted finding is new."""
    return True if f.run_id is None else f.is_new


def echoes_every_finding_once(digest: DriftDigest, findings: list[Finding]) -> CharacteristicResult:
    """One line per finding, no more and no fewer.

    A dropped finding is the failure that matters most: the digest is the only
    place a stakeholder sees it, so a finding that does not make the digest did
    not happen as far as the program is concerned.
    """
    expected = sorted(f.downstream for f in findings)
    got = sorted(line.downstream for line in digest.findings)
    if expected == got:
        return CharacteristicResult(
            name="echoes-every-finding-once",
            passed=True,
            detail=f"all {len(expected)} finding(s) narrated exactly once",
        )
    missing = _multiset_diff(expected, got)
    extra = _multiset_diff(got, expected)
    parts = []
    if missing:
        parts.append(f"dropped {', '.join(missing)}")
    if extra:
        parts.append(f"invented or duplicated {', '.join(extra)}")
    return CharacteristicResult(
        name="echoes-every-finding-once",
        passed=False,
        detail="; ".join(parts) or f"expected {len(expected)} line(s), got {len(got)}",
    )


def _multiset_diff(left: list[str], right: list[str]) -> list[str]:
    remaining = list(right)
    out = []
    for item in left:
        if item in remaining:
            remaining.remove(item)
        else:
            out.append(item)
    return out


def never_re_ranks_severity(digest: DriftDigest, findings: list[Finding]) -> CharacteristicResult:
    """Each line carries the bucket the rules engine assigned, verbatim.

    The whole architecture rests on this: deterministic Python decides severity
    and Claude narrates it. A model that quietly downgrades a red has taken over
    the decision, and no amount of good prose makes that acceptable.
    """
    by_key: dict[str, list[str]] = {}
    for f in findings:
        by_key.setdefault(f.downstream, []).append(f.severity_bucket)

    wrong = []
    for line in digest.findings:
        allowed = by_key.get(line.downstream)
        if allowed is None:
            continue  # an invented key — `echoes_every_finding_once` owns that
        if line.bucket not in allowed:
            wrong.append(
                f"{line.downstream}: rules said {allowed[0]!r}, digest said {line.bucket!r}"
            )
    return CharacteristicResult(
        name="never-re-ranks-severity",
        passed=not wrong,
        detail="; ".join(wrong) if wrong else "every bucket echoed verbatim",
    )


def orders_new_red_first(digest: DriftDigest, findings: list[Finding]) -> CharacteristicResult:
    """New red, then red, then yellow, then white.

    Checked as a non-decreasing rank rather than an exact sequence: the template
    does not order *within* a band, so two reds in either order are both correct
    and asserting one of them would be inventing a requirement.
    """
    newness = {f.downstream: _is_new(f) for f in findings}
    ranks = [
        _rank(line.bucket, newness.get(line.downstream, True))
        for line in digest.findings
        if line.downstream in newness
    ]
    inversions = [
        f"position {i + 1} ({digest.findings[i].bucket}) precedes "
        f"a higher-priority {digest.findings[i + 1].bucket}"
        for i in range(len(ranks) - 1)
        if ranks[i] > ranks[i + 1]
    ]
    return CharacteristicResult(
        name="orders-new-red-first",
        passed=not inversions,
        detail="; ".join(inversions[:3]) if inversions else "ordered by bucket as specified",
    )


def marks_buckets_with_the_right_glyph(digest: DriftDigest) -> CharacteristicResult:
    """🔴 / 🟡 / ⚪, leading the line.

    Cosmetic-looking and not: the digest lands in a program channel where the
    glyph is what gets scanned. A red line that opens with a yellow dot is read
    as yellow no matter what the words say.
    """
    wrong = []
    for line in digest.findings:
        want = GLYPHS.get(line.bucket)
        if want is None:
            wrong.append(f"{line.downstream}: unknown bucket {line.bucket!r}")
        elif not line.line.lstrip().startswith(want):
            leading = line.line.lstrip()[:2].strip()
            wrong.append(
                f"{line.downstream}: {line.bucket} line leads with {leading!r}, not {want}"
            )
    return CharacteristicResult(
        name="marks-buckets-with-the-right-glyph",
        passed=not wrong,
        detail="; ".join(wrong[:3]) if wrong else "every line carries its bucket's glyph",
    )


def no_unsupported_claims(
    digest: DriftDigest, payload: dict
) -> tuple[CharacteristicResult, object]:
    """No invented keys, dates or day counts, checked against the exact payload.

    Scored over the whole digest at once — subject, summary and every line —
    because a fabricated ticket key in the rollup sentence is the same failure
    as one in a finding line.
    """
    report = groundedness.check(rendered(digest), payload)
    return (
        CharacteristicResult(
            name="no-unsupported-claims",
            passed=report.grounded,
            detail=report.summary(),
        ),
        report,
    )


def names_the_project(digest: DriftDigest, project_key: str) -> CharacteristicResult:
    """The subject line names the project.

    The digest is delivered into a channel that may carry more than one program;
    a subject that does not say which one is a subject that has to be opened.
    """
    present = project_key.lower() in digest.subject.lower()
    return CharacteristicResult(
        name="names-the-project",
        passed=present,
        detail=(
            f"subject names {project_key}" if present else f"subject omits {project_key}: "
            f"{digest.subject!r}"
        ),
    )


def summary_states_the_counts(digest: DriftDigest, findings: list[Finding]) -> CharacteristicResult:
    """Advisory. The counts by bucket, however the model chose to word them.

    Digits only. "two red" is a correct summary this check cannot read, which is
    exactly why it cannot gate — see the module docstring.
    """
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity_bucket] = counts.get(f.severity_bucket, 0) + 1
    missing = [
        f"{bucket}={n}" for bucket, n in sorted(counts.items()) if str(n) not in digest.summary
    ]
    return CharacteristicResult(
        name="summary-states-the-counts",
        passed=not missing,
        detail=(
            "every bucket count appears as a digit"
            if not missing
            else f"not stated as digits: {', '.join(missing)} — may be spelled out"
        ),
        advisory=True,
    )


def rendered(digest: DriftDigest) -> str:
    """The whole digest as one string, the way a reader receives it."""
    return "\n".join([digest.subject, digest.summary, *(line.line for line in digest.findings)])
