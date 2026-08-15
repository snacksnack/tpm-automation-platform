"""The frozen inputs the drift digest is scored against (RC1-261).

Six finding sets, chosen to cover the shapes the prompt template makes explicit
promises about rather than to look like a representative week:

* **an ordinary mix** — red, yellow and white together, which is the only case
  where the ordering rule can be violated
* **new-vs-old red** — two reds where only one is new, so "NEW red first" is
  separable from "red first"
* **a single white** — the case where the model is most tempted to inflate a
  minor finding into something worth writing home about
* **resolved-only alongside findings** — the summary must mention what cleared
* **a long tail** — nine findings, where "exactly one line each" starts to cost
  the model something
* **no findings at all** — scored by the free subject, since it never reaches a
  model at all

Frozen because a golden that regenerates its own inputs measures nothing: if the
input moves and the score moves, there is no attribution left to make.
"""

from __future__ import annotations

from datetime import date

from collectors.models import Issue, ProjectSnapshot
from store.models import Finding

#: Both severity bands, so a bucket can be checked against a score rather than
#: taken on faith. Matching `drift/rules.py` is asserted in the eval's own tests.
_RED = 0.85
_YELLOW = 0.55
_WHITE = 0.20


def _issue(key: str, summary: str, owner: str, due: date | None, start: date | None) -> Issue:
    return Issue(
        key=key,
        summary=summary,
        status="In Progress",
        status_category="In Progress",
        priority="High",
        assignee_name=owner,
        due=due,
        start=start,
    )


SNAPSHOT = ProjectSnapshot(
    project_key="RC1",
    issues=[
        _issue("RC1-1", "Vendor security review", "Reid", date(2026, 7, 23), None),
        _issue("RC1-2", "Launch readiness sign-off", "Dana", None, date(2026, 7, 24)),
        _issue("RC1-9", "Analytics dashboard", "Kim", None, date(2026, 7, 30)),
        _issue("RC1-14", "Data migration dry run", "Priya", date(2026, 8, 4), None),
        _issue("RC1-15", "Billing cutover", "Sam", None, date(2026, 8, 5)),
        _issue("RC1-21", "Localisation pass", "Wei", None, date(2026, 8, 11)),
        _issue("RC1-22", "Support runbook", "Jordan", None, date(2026, 8, 12)),
        _issue("RC1-30", "Load test", "Alex", None, date(2026, 8, 18)),
        _issue("RC1-31", "Perf budget review", "Noor", None, date(2026, 8, 19)),
        _issue("RC1-40", "Legal review", "Ada", date(2026, 8, 20), None),
        _issue("RC1-41", "Terms update", "Ivan", None, date(2026, 8, 21)),
    ],
)


def _finding(
    downstream: str,
    upstream: str | None,
    severity: float,
    bucket: str,
    detail: str,
    *,
    is_new: bool = True,
    rule: str = "upstream_slip_unabsorbed",
) -> Finding:
    # `run_id=None` is what `drift_digest._is_new` reads as new. Setting a run_id
    # is how a case says "this one is not new" — the only lever the payload has.
    return Finding(
        rule_type=rule,
        downstream=downstream,
        upstream=upstream,
        severity=severity,
        severity_bucket=bucket,
        detail=detail,
        run_id=None if is_new else 41,
        is_new=is_new,
    )


#: Each entry is (case_id, findings, resolved).
FINDING_SETS: list[tuple[str, list[Finding], list[Finding]]] = [
    (
        "mixed-buckets",
        [
            _finding(
                "RC1-2",
                "RC1-1",
                _RED,
                "red",
                "RC1-1 due moved 2026-07-23 → 2026-08-06 (14d); RC1-2 still starts 2026-07-24",
            ),
            _finding(
                "RC1-9",
                "RC1-1",
                _YELLOW,
                "yellow",
                "RC1-1 slipped 14d; RC1-9 starts 2026-07-30, 6d of slack absorbed",
            ),
            _finding(
                "RC1-22",
                "RC1-21",
                _WHITE,
                "white",
                "RC1-21 slipped 2d; RC1-22 starts 2026-08-12 with 9d slack",
            ),
        ],
        [],
    ),
    (
        "new-red-among-old-red",
        [
            _finding(
                "RC1-15",
                "RC1-14",
                _RED,
                "red",
                "RC1-14 due moved 2026-08-04 → 2026-08-18 (14d); RC1-15 still starts 2026-08-05",
                is_new=False,
            ),
            _finding(
                "RC1-31",
                "RC1-30",
                _RED,
                "red",
                "RC1-30 slipped 2026-08-18 → 2026-08-27 (9d); RC1-31 still starts 2026-08-19",
            ),
        ],
        [],
    ),
    (
        "single-white-only",
        [
            _finding(
                "RC1-22",
                "RC1-21",
                _WHITE,
                "white",
                "RC1-21 slipped 1d; RC1-22 starts 2026-08-12 with 10d slack",
            )
        ],
        [],
    ),
    (
        "findings-and-resolved",
        [
            _finding(
                "RC1-41",
                "RC1-40",
                _RED,
                "red",
                "RC1-40 due moved 2026-08-20 → 2026-08-28 (8d); RC1-41 still starts 2026-08-21",
            ),
        ],
        [
            _finding("RC1-9", "RC1-1", _YELLOW, "yellow", "cleared"),
            _finding("RC1-22", "RC1-21", _WHITE, "white", "cleared"),
        ],
    ),
    (
        "long-tail",
        [
            _finding(
                "RC1-2", "RC1-1", _RED, "red", "RC1-1 slipped 14d; RC1-2 starts 2026-07-24"
            ),
            _finding(
                "RC1-15", "RC1-14", _RED, "red", "RC1-14 slipped 14d; RC1-15 starts 2026-08-05"
            ),
            _finding(
                "RC1-31", "RC1-30", _RED, "red", "RC1-30 slipped 9d; RC1-31 starts 2026-08-19",
                is_new=False,
            ),
            _finding(
                "RC1-9", "RC1-1", _YELLOW, "yellow", "RC1-1 slipped 14d; RC1-9 starts 2026-07-30"
            ),
            _finding(
                "RC1-41", "RC1-40", _YELLOW, "yellow", "RC1-40 slipped 8d; RC1-41 starts 2026-08-21"
            ),
            _finding(
                "RC1-22", "RC1-21", _YELLOW, "yellow",
                "RC1-21 slipped 2d; RC1-22 starts 2026-08-12",
                is_new=False,
            ),
            _finding(
                "RC1-30", "RC1-21", _WHITE, "white", "RC1-21 slipped 2d; RC1-30 starts 2026-08-18"
            ),
            _finding(
                "RC1-21", "RC1-14", _WHITE, "white", "RC1-14 slipped 14d; RC1-21 starts 2026-08-11"
            ),
            _finding(
                "RC1-40", "RC1-14", _WHITE, "white", "RC1-14 slipped 14d; RC1-40 due 2026-08-20",
                is_new=False,
            ),
        ],
        [],
    ),
    # The all-clear. Kept in the same corpus rather than special-cased elsewhere,
    # because "produces nothing" is a behaviour the digest has to get right, not
    # an absence of behaviour. Scored by the free subject.
    ("all-clear", [], []),
    (
        "all-clear-with-resolved",
        [],
        [_finding("RC1-2", "RC1-1", _RED, "red", "cleared")],
    ),
]

BY_ID: dict[str, tuple[list[Finding], list[Finding]]] = {
    case_id: (findings, resolved) for case_id, findings, resolved in FINDING_SETS
}
