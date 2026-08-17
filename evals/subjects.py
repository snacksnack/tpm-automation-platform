"""The two drift-digest subjects (RC1-261).

Split by what they cost, not by what they cover — the same split ADR-0031 made
in the planner repo, for the same reason.

* **`drift-digest-allclear`** is free and deterministic. The empty-findings path
  never reaches a model, so it can be scored on every push with no key and no
  spend. It runs in CI.
* **`drift-digest`** drives the real model over the shipped prompt template. It
  costs money and needs a key, so it stays out of `pytest` and out of CI, and is
  run deliberately.

The free half is not the leftovers. "Produces nothing, correctly" is the case a
digest is most likely to get wrong in the most expensive way — a fabricated
all-clear is a program manager told everything is fine when it is not, and a
fabricated *finding* on a quiet week trains everyone to ignore the channel. It
is also the only case where the right behaviour is to not call the API at all,
which is checkable exactly and for free.
"""

from __future__ import annotations

import time

from agent_evals import groundedness, pricing
from agent_evals.case import Case
from agent_evals.record import CaseResult, CharacteristicResult, SubjectVersion, Usage

from evals import checks
from evals.corpus import BY_ID, FINDING_SETS, SNAPSHOT
from narrative import drift_digest
from narrative.models import DriftDigest

BILLED_NAME = "drift-digest"
FREE_NAME = "drift-digest-allclear"

#: What a genuinely quiet run must not claim to have found.
#:
#: Checked with `negation_aware=True`, and the first run showed why: the shipped
#: all-clear summary is "No dependency drift detected in RC1", which a plain
#: substring scan flags on "drift detected" — a clean sentence failed for
#: containing the negated form of the thing it denies. That exact class of false
#: positive is what the library's negation handling exists for, and reaching for
#: it beat re-deriving it here.
_ALL_CLEAR_FORBIDDEN = ("slipped", "at risk", "blocked", "drift detected", "escalate")


class _ExplodingClient:
    """Any use of this is the failure. The all-clear path must not call the API.

    Asserted with a client rather than by mocking spend, because "did not call
    the model" is the actual promise `drift_digest` makes in its docstring, and
    a test that checks the cost was zero would also pass if the call were made
    and the response thrown away.
    """

    def __init__(self) -> None:
        self.messages = self

    def create(self, **_: object) -> object:
        raise AssertionError("the all-clear path called the model")


def _billed_cases() -> tuple[Case, ...]:
    return tuple(
        Case(
            id=case_id,
            input={"case_id": case_id},
            expect=(
                "echoes-every-finding-once",
                "never-re-ranks-severity",
                "orders-new-red-first",
                "marks-buckets-with-the-right-glyph",
                "no-unsupported-claims",
                "names-the-project",
            ),
            tags=("drift-digest",),
        )
        for case_id, findings, _ in FINDING_SETS
        if findings
    )


def _free_cases() -> tuple[Case, ...]:
    return tuple(
        Case(
            id=case_id,
            input={"case_id": case_id},
            expect=("is-flagged-all-clear", "never-calls-the-model", "invents-no-findings"),
            tags=("drift-digest", "all-clear"),
        )
        for case_id, findings, _ in FINDING_SETS
        if not findings
    )


BILLED_CASES = _billed_cases()
FREE_CASES = _free_cases()


def _template_version() -> str:
    """The prompt is the subject here as much as the code is.

    A hash rather than the version comment, because the comment is a promise to
    bump it by hand and this is the measurement that would be wrong if someone
    forgot. `evals template-drift` compares the two and says so.
    """
    import hashlib

    return f"template-sha256:{hashlib.sha256(drift_digest.load_prompt().encode()).hexdigest()[:12]}"


def _code_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("tpm-automation-platform")
    except PackageNotFoundError:  # running from a source checkout without an install
        return "0.0.0+unknown"


def billed_version(model: str) -> SubjectVersion:
    return SubjectVersion(
        subject=BILLED_NAME,
        code_version=_code_version(),
        model=model,
        prompt_version=_template_version(),
    )


def free_version() -> SubjectVersion:
    return SubjectVersion(
        subject=FREE_NAME,
        code_version=_code_version(),
        # Stated rather than omitted: this path reaching a model is the bug.
        model=None,
        prompt_version=None,
    )


def run_billed(case: Case, client, model: str) -> CaseResult:
    findings, resolved = BY_ID[case.input["case_id"]]
    payload = drift_digest.build_payload(findings, SNAPSHOT, resolved)
    started = time.perf_counter()
    try:
        digest = drift_digest.build_digest(
            findings, SNAPSHOT, resolved=resolved, client=client, model=model
        )
    except Exception as exc:
        return CaseResult(
            case_id=case.id,
            usage=Usage(latency_ms=(time.perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )
    latency_ms = (time.perf_counter() - started) * 1000

    grounded, report = checks.no_unsupported_claims(digest, payload)
    results = [
        checks.echoes_every_finding_once(digest, findings),
        checks.never_re_ranks_severity(digest, findings),
        checks.orders_new_red_first(digest, findings),
        checks.marks_buckets_with_the_right_glyph(digest),
        grounded,
        checks.names_the_project(digest, SNAPSHOT.project_key),
        checks.summary_states_the_counts(digest, findings),
    ]
    return CaseResult(
        case_id=case.id,
        characteristics=results,
        usage=_usage(latency_ms, model),
        observations={
            # The digest itself. A failure that cannot be read afterwards is
            # half a finding.
            "subject": digest.subject,
            "summary": digest.summary,
            "lines": [line.line for line in digest.findings],
            "findings_in": len(findings),
            "lines_out": len(digest.findings),
            "claims_checked": report.checked,
            "violations": len(report.violations),
        },
    )


def _usage(latency_ms: float, model: str) -> Usage:
    """Priced from the digest module's side channel (RC1-269).

    Until v0.3.2 the harness had no price for this subject's model and cost was
    deliberately left unrecorded; a billed run reporting $0 is RC1-254's exact
    finding, so now that a price exists the omission would be the bug. The
    fallback branch keeps the old honesty: no measured tokens, no invented cost.
    """
    used = drift_digest.last_usage
    if used is None:
        return Usage(latency_ms=latency_ms)
    return Usage(
        input_tokens=used.input_tokens,
        output_tokens=used.output_tokens,
        cost_usd=pricing.cost_usd(model, used.input_tokens, used.output_tokens),
        latency_ms=latency_ms,
    )


def run_free(case: Case) -> CaseResult:
    findings, resolved = BY_ID[case.input["case_id"]]
    started = time.perf_counter()
    try:
        digest = drift_digest.build_digest(
            findings, SNAPSHOT, resolved=resolved, client=_ExplodingClient()
        )
        called = False
    except AssertionError as exc:
        return CaseResult(
            case_id=case.id,
            characteristics=[
                CharacteristicResult(
                    name="never-calls-the-model", passed=False, detail=str(exc)
                )
            ],
            usage=Usage(latency_ms=(time.perf_counter() - started) * 1000),
        )
    latency_ms = (time.perf_counter() - started) * 1000

    said = groundedness.must_not_say(
        _text(digest), list(_ALL_CLEAR_FORBIDDEN), negation_aware=True
    )
    results = [
        CharacteristicResult(
            name="is-flagged-all-clear",
            passed=digest.all_clear and not digest.findings,
            detail=(
                "all_clear set, no lines"
                if digest.all_clear and not digest.findings
                else f"all_clear={digest.all_clear}, {len(digest.findings)} line(s)"
            ),
        ),
        CharacteristicResult(
            name="never-calls-the-model",
            passed=not called,
            detail="no API call on the empty path",
        ),
        CharacteristicResult(
            name="invents-no-findings",
            passed=not said,
            detail=(
                f"names drift that does not exist: {said[0].detail}"
                if said
                else "reports the quiet run as quiet"
            ),
        ),
        CharacteristicResult(
            name="mentions-what-resolved",
            passed=(not resolved) or (str(len(resolved)) in digest.summary),
            detail=(
                "nothing resolved to mention"
                if not resolved
                else f"{len(resolved)} resolved, summary={digest.summary!r}"
            ),
            advisory=True,
        ),
    ]
    return CaseResult(
        case_id=case.id,
        characteristics=results,
        usage=Usage(latency_ms=latency_ms),
        observations={"subject": digest.subject, "summary": digest.summary},
    )


def _text(digest: DriftDigest) -> str:
    return checks.rendered(digest)
