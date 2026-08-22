"""Simulator tests — offline, against an in-memory Jira (RC1-299).

The property the story is built on: converging day by day and jumping
straight to a day leave Jira in the same state, and a converged day verifies
with nothing outstanding.
"""

from __future__ import annotations

import copy
import re
from collections import defaultdict
from pathlib import Path

import pytest

from seed.jira_client import FLAGGED_FIELD, POINTS_FIELD, START_DATE_FIELD
from simulate import apply, scenario
from simulate.clock import SimState
from simulate.scenario import (
    GA_DAY,
    LAST_DAY,
    PROGRAM_LABEL,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_REVIEW,
    STATUS_TODO,
    state_at,
)


class FakeJira:
    """Just enough of Jira for `simulate.apply`: issues as search-shaped dicts."""

    def __init__(self) -> None:
        self.issues: dict[str, dict] = {}
        self.comments: dict[str, list[str]] = defaultdict(list)
        self.n = 0

    def my_account_id(self) -> str:
        return "acct-1"

    def _new(self, kind: str, summary: str, labels, *, due, start, parent) -> str:
        self.n += 1
        key = f"PMA-{self.n}"
        self.issues[key] = {
            "key": key,
            "fields": {
                "issuetype": {"name": kind},
                "summary": summary,
                "status": {"name": STATUS_TODO},
                "labels": list(labels),
                "duedate": due,
                START_DATE_FIELD: start,
                POINTS_FIELD: None,
                FLAGGED_FIELD: None,
                "issuelinks": [],
                "parent": {"key": parent} if parent else None,
            },
        }
        return key

    def search(self, jql: str, fields):
        out = []
        label = re.search(r'labels = "([^"]+)"', jql)
        parent = re.search(r"parent = (\S+)", jql)
        for issue in self.issues.values():
            f = issue["fields"]
            if label and label.group(1) not in f["labels"]:
                continue
            if parent and (f["parent"] or {}).get("key") != parent.group(1):
                continue
            out.append(copy.deepcopy(issue))
        return out

    def create_epic(self, summary, labels, *, project, due=None):
        return self._new("Epic", summary, labels, due=due, start=None, parent=None)

    def create_story(self, summary, labels, *, due, start, assignee_id, parent, project,
                     description=None):
        return self._new("Story", summary, labels, due=due, start=start, parent=parent)

    def set_fields(self, key, fields):
        self.issues[key]["fields"].update(fields)

    def set_estimation(self, key, points, board_id):
        self.issues[key]["fields"][POINTS_FIELD] = float(points)

    def set_flagged(self, key, flagged):
        self.issues[key]["fields"][FLAGGED_FIELD] = [{"value": "Impediment"}] if flagged else None

    def add_labels(self, key, labels):
        cur = self.issues[key]["fields"]["labels"]
        cur.extend(lb for lb in labels if lb not in cur)

    def remove_labels(self, key, labels):
        f = self.issues[key]["fields"]
        f["labels"] = [lb for lb in f["labels"] if lb not in labels]

    def transition_to(self, key, status_name):
        f = self.issues[key]["fields"]
        if f["status"]["name"] == status_name:
            return False
        f["status"] = {"name": status_name}
        return True

    def add_comment(self, key, text):
        self.comments[key].append(text)

    def create_blocks_link(self, blocker, blocked):
        self.issues[blocker]["fields"]["issuelinks"].append(
            {"type": {"name": "Blocks"}, "outwardIssue": {"key": blocked}}
        )
        self.issues[blocked]["fields"]["issuelinks"].append(
            {"type": {"name": "Blocks"}, "inwardIssue": {"key": blocker}}
        )

    def delete_issue(self, key):
        del self.issues[key]


def _quiet(*_args, **_kw):
    pass


def converge(jira: FakeJira, day: int, **kw) -> apply.Report:
    return apply.converge(jira, day, board_id=68, log=_quiet, **kw)


def snapshot(jira: FakeJira) -> dict[str, tuple]:
    """Observable state per slug, independent of keys."""
    by_key = {}
    for key, issue in jira.issues.items():
        slug = apply._slug_of(issue["fields"]["labels"])
        by_key[key] = slug
    out = {}
    for key, issue in jira.issues.items():
        f = issue["fields"]
        blocks = tuple(sorted(
            by_key[lk["outwardIssue"]["key"]]
            for lk in f["issuelinks"] if "outwardIssue" in lk
        ))
        out[by_key[key]] = (
            f["status"]["name"], f["duedate"], f[START_DATE_FIELD], f[POINTS_FIELD],
            bool(f[FLAGGED_FIELD]), tuple(sorted(f["labels"])), blocks,
        )
    return out


# --- the scenario itself -------------------------------------------------------------


def test_scenario_shape():
    assert len(scenario.STORIES) == 34
    assert len({s.slug for s in scenario.STORIES}) == 34
    assert scenario.baseline_points() == 135
    assert scenario.added_points() == 16
    assert scenario.BASE_POINTS_PLAN_PER_WEEK == scenario.baseline_points() / scenario.WEEKS
    assert round(scenario.added_points() / scenario.baseline_points(), 3) == 0.119


def test_every_story_is_internally_consistent():
    for s in scenario.STORIES:
        assert s.created <= s.start, s.slug
        assert s.started_day <= s.done, s.slug
        assert s.done <= GA_DAY, s.slug
        assert s.owner in scenario.OWNERS, s.slug
        for b in s.blocks:
            assert b in scenario.BY_SLUG, f"{s.slug} blocks unknown {b}"
        if s.slip:
            assert s.slip[1] > s.due and s.slip[0] > s.created, s.slug
        if s.flagged:
            assert s.flagged[0] < s.flagged[1], s.slug


def test_links_form_a_dag_rooted_at_ga():
    graph = defaultdict(set)
    for up, down in scenario.links():
        graph[up].add(down)
    seen, stack = set(), set()

    def visit(n):
        assert n not in stack, f"cycle through {n}"
        if n in seen:
            return
        stack.add(n)
        for m in graph[n]:
            visit(m)
        stack.discard(n)
        seen.add(n)

    for n in list(graph):
        visit(n)
    assert scenario.BY_SLUG["p-ga"].ga_blocking
    assert any(down == "p-ga" for _, down in scenario.links())


def _slack(day: int, up: str, down: str) -> int:
    st = state_at(day)
    return (st.issues[down].start - st.issues[up].due).days


def test_baseline_slack_is_tight_but_positive_and_the_slip_inverts_one_chain():
    day0 = [_slack(0, up, down) for up, down in scenario.links()
            if up in state_at(0).issues and down in state_at(0).issues]
    assert min(day0) == 3
    assert _slack(28, "t-context", "s-latency") == 3
    assert _slack(29, "t-context", "s-latency") == -11
    # nothing else moves on the slip day
    others = [(up, down) for up, down in scenario.links() if up != "t-context"]
    assert all(_slack(28, u, d) == _slack(29, u, d) for u, d in others
               if u in state_at(28).issues and d in state_at(28).issues)


def test_status_is_monotonic_and_review_only_for_long_stories():
    order = {STATUS_TODO: 0, STATUS_IN_PROGRESS: 1, STATUS_REVIEW: 2, STATUS_DONE: 3}
    for s in scenario.STORIES:
        seq = [order[s.status_on(d)] for d in range(s.created, LAST_DAY + 1)]
        assert seq == sorted(seq), s.slug
        assert s.status_on(s.done) == STATUS_DONE
        if s.done - s.started_day >= 4:
            assert s.status_on(s.done - 1) == STATUS_REVIEW, s.slug
        else:
            assert STATUS_REVIEW not in {s.status_on(d) for d in range(0, LAST_DAY + 1)}, s.slug


def test_planted_events_are_visible_in_the_state_on_their_day():
    # scope add: four stories appear on days 16-17 and not before
    assert len(state_at(15).issues) == 30
    assert len(state_at(16).issues) == 33
    assert len(state_at(17).issues) == 34
    assert {e.id for e in state_at(16).events} == {"scope-add"}
    # slip: the due date moves on day 29, downstream untouched
    assert state_at(28).issues["t-context"].due == scenario.sim_date(27)
    assert state_at(29).issues["t-context"].due == scenario.sim_date(41)
    assert state_at(29).issues["s-latency"].start == scenario.sim_date(30)
    # cost spike: the week-6 row lands on day 42 at ~2x plan
    assert len(state_at(41).spend) == 5
    row = state_at(42).spend[-1]
    assert row.week == 6 and round(row.actual_usd / row.planned_usd, 2) == 2.04
    # source break: program label gone 43-47, back on 48; slug labels never move
    for day in (42, 48):
        assert all(PROGRAM_LABEL in i.labels for i in state_at(day).issues.values())
        assert PROGRAM_LABEL in state_at(day).epic_labels
    for day in (43, 47):
        assert not any(PROGRAM_LABEL in i.labels for i in state_at(day).issues.values())
        assert PROGRAM_LABEL not in state_at(day).epic_labels
        assert all(scenario.slug_label(i.slug) in i.labels for i in state_at(day).issues.values())
    assert "source-break" in {e.id for e in state_at(45).events}
    assert "source-break" not in {e.id for e in state_at(48).events}


def test_every_event_names_kpis_from_the_adopted_tree():
    adopted = {
        "forecast-slip-days", "cost-vs-envelope", "scope-change-pct",
        "critical-path-slack-days", "blocked-share-pct", "weekly-spend-burn-ratio",
    }
    for e in scenario.EVENTS:
        assert set(e.must_move) <= adopted, e.id


def test_program_completes_on_the_ga_day():
    assert state_at(GA_DAY).points_done == state_at(GA_DAY).points_total == 151
    assert state_at(GA_DAY - 1).issues["p-ga"].status != STATUS_DONE
    with pytest.raises(ValueError):
        state_at(LAST_DAY + 1)


# --- converging -----------------------------------------------------------------------


def test_seed_creates_the_day_zero_program_and_then_has_nothing_to_do():
    jira = FakeJira()
    report = converge(jira, 0)
    assert report.changed
    assert len(jira.issues) == 31  # epic + 30 baseline stories
    assert apply.verify(jira, 0) == []
    assert not converge(jira, 0).changed


def test_day_by_day_equals_jumping_straight_there():
    stepwise, jump = FakeJira(), FakeJira()
    for day in range(0, LAST_DAY + 1):
        converge(stepwise, day)
        assert apply.verify(stepwise, day) == [], f"day {day} does not verify"
    converge(jump, LAST_DAY)
    assert snapshot(stepwise) == snapshot(jump)
    assert apply.verify(jump, LAST_DAY) == []


def test_points_dates_links_and_flags_land():
    jira = FakeJira()
    converge(jira, 35)
    snap = snapshot(jira)
    status, due, start, points, flagged, labels, blocks = snap["t-context"]
    assert points == 8.0 and due == scenario.sim_date(41).isoformat()
    assert blocks == ("s-latency",)
    assert snap["s-latency"][4] is True  # flagged while waiting on t-context
    assert snap["p-security"][4] is False  # its flag cleared on day 33
    assert snap["t-sdk"][0] == STATUS_DONE
    assert "x-mobile" in snap and snap["x-mobile"][0] == STATUS_DONE
    assert "own-tomas" in snap["p-infra"][5]


def test_the_slip_is_a_second_write_with_a_comment_and_the_break_is_silent():
    jira = FakeJira()
    for day in range(0, 44):
        converge(jira, day)
    key = next(k for k, i in jira.issues.items() if "ks-t-context" in i["fields"]["labels"])
    assert any("Due moved" in c for c in jira.comments[key])
    # day 43: every story lost the program label, and nobody said anything
    assert not any(PROGRAM_LABEL in i["fields"]["labels"] for i in jira.issues.values())
    assert not any("label" in c.lower() for cs in jira.comments.values() for c in cs)
    converge(jira, 48)
    assert all(PROGRAM_LABEL in i["fields"]["labels"] for i in jira.issues.values())


def test_jumping_to_a_slipped_day_still_writes_the_planned_due_first():
    jira = FakeJira()
    report = converge(jira, 40)
    assert any(a.kind == "due" and a.slug == "t-context" for a in report.actions)
    key = next(k for k, i in jira.issues.items() if "ks-t-context" in i["fields"]["labels"])
    assert jira.issues[key]["fields"]["duedate"] == scenario.sim_date(41).isoformat()
    assert any("Due moved" in c for c in jira.comments[key])


def test_dry_run_touches_nothing():
    jira = FakeJira()
    report = converge(jira, 10, dry_run=True)
    assert report.changed and report.dry_run
    assert jira.issues == {}


def test_teardown_removes_everything_and_is_safe_to_repeat():
    jira = FakeJira()
    converge(jira, 20)
    assert apply.teardown(jira, log=_quiet) == 35  # all 34 stories by day 20 + epic
    assert jira.issues == {}
    assert apply.teardown(jira, log=_quiet) == 0
    converge(jira, 0)
    assert len(jira.issues) == 31


def test_verify_reports_drift_a_human_introduced():
    jira = FakeJira()
    converge(jira, 12)
    key = next(k for k, i in jira.issues.items() if "ks-t-gateway" in i["fields"]["labels"])
    jira.transition_to(key, STATUS_TODO)
    jira.remove_labels(key, [PROGRAM_LABEL])
    outstanding = apply.verify(jira, 12)
    assert {(a.kind, a.slug) for a in outstanding} == {
        ("status", "t-gateway"), ("label_add", "t-gateway"),
    }


# --- the clock ------------------------------------------------------------------------


def test_clock_manifest_and_spend_line(tmp_path: Path):
    state = SimState(tmp_path / "sim")
    assert state.read_clock() is None
    state.write(42, {"epic": "PMA-1", "t-sdk": "PMA-2"})
    clock = state.read_clock()
    assert clock.day == 42 and clock.sim_date == scenario.sim_date(42)
    rows = state.spend_path.read_text().splitlines()
    assert rows[0].startswith("week,")
    assert len(rows) == 1 + 6  # weeks 1-6 have landed by day 42
    assert rows[-1].startswith("6,") and ",2450.00," in rows[-1]
    assert '"epic": "PMA-1"' in state.manifest_path.read_text()
    state.forget()
    assert not state.clock_path.exists() and not (tmp_path / "sim").exists()


# --- the CLI, wired to the fake ---------------------------------------------------------


def test_cli_flow(monkeypatch, tmp_path: Path, capsys):
    from simulate import __main__ as cli

    jira = FakeJira()

    class _Ctx:
        def __enter__(self):
            return jira

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(cli, "_jira", lambda: _Ctx())
    monkeypatch.setattr(cli.settings, "kpi_sim_dir", str(tmp_path / "sim"))

    assert cli.main(["tick"]) == 1  # no clock yet
    assert cli.main(["seed"]) == 0
    assert cli.main(["tick", "--days", "16"]) == 0
    assert cli.main(["status"]) == 0
    assert "day 16" in capsys.readouterr().out and len(jira.issues) == 34
    assert cli.main(["verify"]) == 0
    assert cli.main(["to-day", "69"]) == 0
    assert cli.main(["tick"]) == 1  # program over
    assert cli.main(["teardown"]) == 0
    assert jira.issues == {} and cli.main(["status"]) == 0
    assert "not seeded" in capsys.readouterr().out
