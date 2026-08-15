"""Can these goldens actually fail? Ask the prompt (RC1-261).

A suite that passes on its first run has told you one of two things and does not
say which: the subject is good, or the checks are asleep. This is how the
question gets settled — take one hard rule out of the prompt template, run the
same cases, and see whether the characteristic that rule exists to enforce goes
red.

It is the same instinct as the planner repo's `evals construct`: a validity
check that needs no labels and no human, because the *relative* result is known
by construction. There, a clean output must outrank a planted one. Here, a
prompt missing its ordering rule must score worse on ordering than one that has
it. Neither proves the subject is good. Both prove the measurement is awake.

## Reading the result

* **detects** — the characteristic failed when its rule was removed. The check
  is load-bearing and the rule is doing work.
* **NO SIGNAL** — the characteristic passed anyway. That is not automatically a
  bug: a model may follow a convention the prompt no longer states, especially
  an obvious one. What it does mean is that this characteristic is not currently
  evidence that the *prompt* is right, and a regression in that rule would not
  be caught here. Worth knowing before trusting it as a gate.

The cost is real — one API call per degradation per case — so this is run
deliberately when the prompt or the checks change, not on a schedule.
"""

from __future__ import annotations

from dataclasses import dataclass

from narrative import drift_digest

#: The default cases to degrade against: the smallest case carrying all three
#: buckets, and the one where "one line each" costs the model real effort.
CASES = ("mixed-buckets", "long-tail")


@dataclass(frozen=True)
class Degradation:
    """One hard rule removed, and the characteristic it should take with it."""

    name: str
    #: Verbatim text to strip from the template. Asserted to be present before
    #: the run — a degradation that silently matches nothing would come back
    #: "NO SIGNAL" and look like a finding about the model.
    remove: str
    expect_breaks: str
    #: The cases that can actually discriminate this rule, where the default set
    #: cannot.
    #:
    #: The ordering rule needed this and the first run did not have it. Every
    #: default case arrives from `build_payload` already sorted by severity, so
    #: preserving the input order passes — the rule was being tested only where
    #: obeying it required doing nothing. `new-red-among-old-red` is the case
    #: that arrives *wrong* (stale red first, because the sort ignores newness)
    #: and so is the only one that asks the model to reorder.
    cases: tuple[str, ...] = CASES


DEGRADATIONS: tuple[Degradation, ...] = (
    Degradation(
        name="no-severity-rule",
        remove=(
            "- NEVER soften, inflate, or re-rank severity. Use each finding's given bucket\n"
            "  verbatim. Report red as red even if it seems minor.\n"
        ),
        expect_breaks="never-re-ranks-severity",
    ),
    Degradation(
        name="no-ordering-rule",
        remove=(
            "- Order findings: all NEW red first, then remaining red, then yellow, then white.\n"
        ),
        expect_breaks="orders-new-red-first",
        cases=("new-red-among-old-red", "long-tail"),
    ),
    Degradation(
        name="no-glyph-rule",
        remove="- Lead each red line with 🔴, yellow with 🟡, white with ⚪.\n",
        expect_breaks="marks-buckets-with-the-right-glyph",
    ),
    Degradation(
        name="no-one-line-rule",
        remove=(
            "- Write exactly ONE line per finding. Reference the real downstream key and the\n"
            "  concrete dates/keys from that finding's `detail`. Name the owner if present.\n"
        ),
        expect_breaks="echoes-every-finding-once",
    ),
    Degradation(
        name="no-invention-rule",
        remove=(
            "- NEVER invent findings, tickets, keys, dates, or owners. Use only what is in the\n"
            "  payload. If a fact is not present, omit it — do not guess.\n"
        ),
        expect_breaks="no-unsupported-claims",
    ),
)


def degraded_prompt(degradation: Degradation) -> str:
    """The template with one rule removed. Raises if the rule is not there.

    Verbatim matching, deliberately: a fuzzy strip that quietly removed the
    wrong lines would produce a result about a prompt nobody has read.
    """
    text = drift_digest.load_prompt()
    if degradation.remove not in text:
        raise ValueError(
            f"{degradation.name}: the text to remove is not in the template verbatim. "
            "The template changed — update the degradation to match it, or this run "
            "would report on a prompt it never actually altered."
        )
    return text.replace(degradation.remove, "")
