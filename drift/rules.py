"""Drift detection rules + severity scoring (RC1-137 [6/9]).

Deterministic detection — no LLM decides what's drifting. Each rule is a pure
function over (graph, current snapshot, previous snapshot) -> list[Finding].
Claude's job ([7/9]) is only to narrate the findings these rules produce.

The four rules:
  1. timeline_inversion  upstream due lands after downstream start/due
  2. unabsorbed_slip     upstream due moved back >= N days and downstream hasn't reacted
  3. lead_time_risk      upstream not started and downstream starts inside its lead window
  4. transitive_risk     a transitive blocker entered Blocked since the last run

Severity = days_of_overlap * downstream_priority_weight * proximity_factor,
bucketed red / yellow / white. All knobs live in DriftConfig — no scattered
constants. `today` is injected (never date.today()) so runs are reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from collectors.models import ProjectSnapshot
from drift.graph import DependencyGraph
from store.models import Finding


@dataclass(frozen=True)
class DriftConfig:
    # Rule 2: minimum upstream due-date slip (days) to count as unabsorbed.
    unabsorbed_slip_min_days: int = 3
    # Rule 3: lead-time window in working days, per downstream priority.
    lead_time_days: dict[str, int] = field(
        default_factory=lambda: {"Highest": 7, "High": 5, "Medium": 3, "Low": 2, "Lowest": 1}
    )
    default_lead_time_days: int = 3
    # Severity: priority weights and bucket thresholds.
    priority_weights: dict[str, float] = field(
        default_factory=lambda: {
            "Highest": 3.0, "High": 2.0, "Medium": 1.0, "Low": 0.5, "Lowest": 0.25
        }
    )
    default_priority_weight: float = 1.0
    red_threshold: float = 20.0
    yellow_threshold: float = 6.0


DEFAULT_CONFIG = DriftConfig()

BUCKET_EMOJI = {"red": "🔴", "yellow": "🟡", "white": "⚪"}


# --------------------------------------------------------------------------- #
# Scoring helpers
# --------------------------------------------------------------------------- #
def _weight(priority: str | None, cfg: DriftConfig) -> float:
    return cfg.priority_weights.get(priority or "", cfg.default_priority_weight)


def _proximity(when: date, today: date) -> float:
    """Sooner collisions score higher."""
    days = (when - today).days
    if days <= 0:
        return 3.0
    if days <= 7:
        return 2.0
    if days <= 21:
        return 1.5
    return 1.0


def _score(days: int, priority: str | None, when: date, today: date, cfg: DriftConfig):
    severity = round(days * _weight(priority, cfg) * _proximity(when, today), 2)
    if severity >= cfg.red_threshold:
        bucket = "red"
    elif severity >= cfg.yellow_threshold:
        bucket = "yellow"
    else:
        bucket = "white"
    return severity, bucket


def _working_days_between(start: date, end: date) -> int:
    """Signed count of weekdays between start and end (holidays ignored for MVP)."""
    if start == end:
        return 0
    step = 1 if end > start else -1
    day, count = start, 0
    while day != end:
        day += timedelta(days=step)
        if day.weekday() < 5:
            count += step
    return count


# --------------------------------------------------------------------------- #
# Rules — each pure: (graph, current, previous, cfg) -> list[Finding]
# --------------------------------------------------------------------------- #
def timeline_inversion(
    graph: DependencyGraph,
    current: ProjectSnapshot,
    previous: ProjectSnapshot | None = None,
    cfg: DriftConfig = DEFAULT_CONFIG,
    *,
    today: date,
) -> list[Finding]:
    out: list[Finding] = []
    for up_key, down_key in graph.graph.edges():
        up, down = graph.issue(up_key), graph.issue(down_key)
        if up is None or down is None or up.due is None:
            continue
        ref = down.start or down.due
        if ref is None or up.due <= ref:
            continue
        days = (up.due - ref).days
        severity, bucket = _score(days, down.priority, ref, today, cfg)
        out.append(
            Finding(
                rule_type="timeline_inversion", upstream=up_key, downstream=down_key,
                severity=severity, severity_bucket=bucket,
                detail=f"{up_key} due {up.due} lands after {down_key} start/due {ref} "
                f"({days}d overlap).",
            )
        )
    return out


def unabsorbed_slip(
    graph: DependencyGraph,
    current: ProjectSnapshot,
    previous: ProjectSnapshot | None = None,
    cfg: DriftConfig = DEFAULT_CONFIG,
    *,
    today: date,
) -> list[Finding]:
    out: list[Finding] = []
    for up_key, down_key in graph.graph.edges():
        up, down = graph.issue(up_key), graph.issue(down_key)
        if up is None or down is None:
            continue
        slips = [
            dc for dc in up.date_changes
            if dc.field == "duedate" and dc.from_date and dc.to_date and dc.to_date > dc.from_date
        ]
        if not slips:
            continue
        slip = max(slips, key=lambda dc: dc.changed_at)
        slip_days = (slip.to_date - slip.from_date).days
        if slip_days < cfg.unabsorbed_slip_min_days:
            continue
        # Downstream "absorbed" the slip if its dates were touched at/after the slip.
        down_touch = max((dc.changed_at for dc in down.date_changes), default=None)
        if down_touch is not None and down_touch >= slip.changed_at:
            continue
        when = down.due or down.start or slip.to_date
        severity, bucket = _score(slip_days, down.priority, when, today, cfg)
        out.append(
            Finding(
                rule_type="unabsorbed_slip", upstream=up_key, downstream=down_key,
                severity=severity, severity_bucket=bucket,
                detail=f"{up_key} due slipped {slip.from_date}->{slip.to_date} ({slip_days}d); "
                f"{down_key} dates unchanged since.",
            )
        )
    return out


def lead_time_risk(
    graph: DependencyGraph,
    current: ProjectSnapshot,
    previous: ProjectSnapshot | None = None,
    cfg: DriftConfig = DEFAULT_CONFIG,
    *,
    today: date,
) -> list[Finding]:
    out: list[Finding] = []
    for up_key, down_key in graph.graph.edges():
        up, down = graph.issue(up_key), graph.issue(down_key)
        if up is None or down is None or not up.not_started or down.start is None:
            continue
        window = cfg.lead_time_days.get(down.priority or "", cfg.default_lead_time_days)
        days_until = _working_days_between(today, down.start)
        if days_until > window:
            continue
        days = max(1, window - days_until)
        severity, bucket = _score(days, down.priority, down.start, today, cfg)
        out.append(
            Finding(
                rule_type="lead_time_risk", upstream=up_key, downstream=down_key,
                severity=severity, severity_bucket=bucket,
                detail=f"{up_key} not started; {down_key} starts {down.start} "
                f"(~{days_until} working days, within {window}-day lead time).",
            )
        )
    return out


def transitive_risk(
    graph: DependencyGraph,
    current: ProjectSnapshot,
    previous: ProjectSnapshot | None = None,
    cfg: DriftConfig = DEFAULT_CONFIG,
    *,
    today: date,
) -> list[Finding]:
    out: list[Finding] = []
    for down in current.issues:
        for b_key in graph.transitive_blockers(down.key):
            blocker = graph.issue(b_key)
            if blocker is None or blocker.status != "Blocked":
                continue
            prev_blocker = previous.issue(b_key) if previous else None
            if prev_blocker is not None and prev_blocker.status == "Blocked":
                continue  # already Blocked last run — not new, don't re-alert
            when = down.start or down.due or today
            severity, bucket = _score(1, down.priority, when, today, cfg)
            out.append(
                Finding(
                    rule_type="transitive_risk", upstream=b_key, downstream=down.key,
                    severity=severity, severity_bucket=bucket,
                    detail=f"Transitive blocker {b_key} entered Blocked; {down.key} is downstream.",
                )
            )
    return out


RULES = (timeline_inversion, unabsorbed_slip, lead_time_risk, transitive_risk)


def detect_all(
    graph: DependencyGraph,
    current: ProjectSnapshot,
    previous: ProjectSnapshot | None = None,
    cfg: DriftConfig = DEFAULT_CONFIG,
    *,
    today: date,
) -> list[Finding]:
    """Run every rule and return findings sorted by severity, most severe first."""
    findings: list[Finding] = []
    for rule in RULES:
        findings.extend(rule(graph, current, previous, cfg, today=today))
    findings.sort(key=lambda f: f.severity, reverse=True)
    return findings
