"""Converge live Jira to `scenario.state_at(day)`, and verify it (RC1-299).

One function does the thinking: `diff(state, observed)` lists every action
that would make the observed issues match the expected state. `converge`
applies that list; `verify` reports it. So "seed", "tick", and "does Jira
match day N" are the same computation, and a converged day always verifies —
the done-when of the story is a property of the design, not a test that
happened to pass.

Idempotent by label: stories are found by their `ks-<slug>` label under the
program epic (found by `ks-epic`), never by the program label the collector
keys on — that one is deliberately dropped during the source break, and the
simulator must keep working through it.

The client is duck-typed (`seed.jira_client.JiraClient` or the test fake);
this module never builds a request itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from seed.jira_client import BLOCKS_LINK, FLAGGED_FIELD, POINTS_FIELD, START_DATE_FIELD
from simulate import scenario
from simulate.scenario import (
    EPIC_SLUG,
    PROGRAM_LABEL,
    PROJECT,
    IssueState,
    ProgramState,
    Story,
    slug_label,
)

ISSUE_FIELDS = [
    "summary", "status", "labels", "duedate", START_DATE_FIELD, POINTS_FIELD, FLAGGED_FIELD,
    "issuelinks", "parent",
]


class Jira(Protocol):
    def my_account_id(self) -> str: ...
    def search(self, jql: str, fields: list[str]) -> list[dict]: ...
    def create_epic(self, summary: str, labels: list[str], *, project: str, due: str | None) -> str: ...  # noqa: E501
    def create_story(self, summary: str, labels: list[str], *, due: str | None, start: str | None, assignee_id: str | None, parent: str | None, project: str, description: str | None) -> str: ...  # noqa: E501
    def set_fields(self, key: str, fields: dict[str, object]) -> None: ...
    def set_estimation(self, key: str, points: float, board_id: int) -> None: ...
    def set_flagged(self, key: str, flagged: bool) -> None: ...
    def add_labels(self, key: str, labels: list[str]) -> None: ...
    def remove_labels(self, key: str, labels: list[str]) -> None: ...
    def transition_to(self, key: str, status_name: str) -> bool: ...
    def add_comment(self, key: str, text: str) -> None: ...
    def create_blocks_link(self, blocker: str, blocked: str) -> None: ...
    def delete_issue(self, key: str) -> None: ...


# --- what Jira currently shows -----------------------------------------------------------


@dataclass
class Observed:
    key: str
    slug: str
    status: str
    due: str | None
    start: str | None
    points: float | None
    flagged: bool
    labels: set[str]
    blocks: set[str]  # keys this issue blocks (outward Blocks links)


def _slug_of(labels: list[str]) -> str | None:
    prefix = scenario.SLUG_PREFIX
    return next((lb[len(prefix):] for lb in labels if lb.startswith(prefix)), None)


def observe(issue: dict) -> Observed | None:
    f = issue["fields"]
    slug = _slug_of(f.get("labels") or [])
    if slug is None:
        return None
    blocks = {
        link["outwardIssue"]["key"]
        for link in f.get("issuelinks") or []
        if link.get("type", {}).get("name") == BLOCKS_LINK and "outwardIssue" in link
    }
    return Observed(
        key=issue["key"],
        slug=slug,
        status=f["status"]["name"],
        due=f.get("duedate"),
        start=f.get(START_DATE_FIELD),
        points=f.get(POINTS_FIELD),
        flagged=bool(f.get(FLAGGED_FIELD)),
        labels=set(f.get("labels") or []),
        blocks=blocks,
    )


@dataclass
class Epic:
    key: str
    due: str | None
    labels: set[str]


def find_epic(jira: Jira) -> Epic | None:
    found = jira.search(
        f'project = {PROJECT} AND labels = "{slug_label(EPIC_SLUG)}"', ["duedate", "labels"]
    )
    if not found:
        return None
    f = found[0]["fields"]
    return Epic(found[0]["key"], f.get("duedate"), set(f.get("labels") or []))


def load(jira: Jira, epic_key: str) -> dict[str, Observed]:
    issues = jira.search(f"parent = {epic_key}", ISSUE_FIELDS)
    out: dict[str, Observed] = {}
    for issue in issues:
        obs = observe(issue)
        if obs is not None:
            out[obs.slug] = obs
    return out


# --- the diff --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    kind: str  # create_epic | epic_due | epic_label_add | epic_label_remove | create | due |
    # start | points | label_add | label_remove | link | flag | status
    slug: str  # story slug, or "epic"
    value: object = None
    comment: str | None = None

    def __str__(self) -> str:
        v = "" if self.value is None else f" -> {self.value}"
        return f"{self.kind:<18} {self.slug}{v}"


def _iso(d) -> str:
    return d.isoformat()


def _transition_comment(story: Story, state: IssueState, day: int) -> str:
    when = f"sim day {day}, {scenario.sim_date(day).isoformat()}"
    if state.status == scenario.STATUS_IN_PROGRESS:
        return f"{story.owner} picked this up ({when})."
    if state.status == scenario.STATUS_REVIEW:
        return f"In review — {story.owner} ({when})."
    if state.status == scenario.STATUS_DONE:
        return f"Done — {story.owner} ({when})."
    return f"Moved to {state.status} ({when})."


def _slip_comment(story: Story, day: int) -> str:
    assert story.slip
    old, new = scenario.sim_date(story.due), scenario.sim_date(story.slip[1])
    return (
        f"Due moved {old.isoformat()} -> {new.isoformat()}: async context propagation needs "
        f"a client-library upgrade first; {story.owner} re-planned (sim day {day})."
    )


def _flag_comment(story: Story, day: int, flagged: bool) -> str:
    if flagged:
        why = story.note or "blocked on an upstream"
        return f"Flagged as an impediment: {why} (sim day {day})."
    return f"Impediment cleared (sim day {day})."


def diff(state: ProgramState, epic: Epic | None, observed: dict[str, Observed]) -> list[Action]:
    """Every action that makes `observed` match `state`. Creates come first so that
    links and per-field updates can assume every story exists."""
    actions: list[Action] = []
    if epic is None:
        actions.append(Action("create_epic", "epic", state.epic_summary))
    else:
        if epic.due != _iso(state.epic_due):
            actions.append(Action("epic_due", "epic", _iso(state.epic_due)))
        for lb in sorted(state.epic_labels - epic.labels):
            actions.append(Action("epic_label_add", "epic", lb))
        if PROGRAM_LABEL in epic.labels and PROGRAM_LABEL not in state.epic_labels:
            actions.append(Action("epic_label_remove", "epic", PROGRAM_LABEL))

    for slug, st in state.issues.items():
        if slug not in observed:
            actions.append(Action("create", slug, st.summary))

    for slug, st in state.issues.items():
        story = scenario.BY_SLUG[slug]
        obs = observed.get(slug)
        fresh = obs is None
        # A just-created story carries its create-time fields; compare the rest
        # against the values creation sets so the same diff serves both paths.
        cur_due = _iso(st.due) if fresh else obs.due
        cur_start = _iso(st.start) if fresh else obs.start
        cur_points = None if fresh else obs.points
        cur_flag = False if fresh else obs.flagged
        cur_labels = set(st.labels) if fresh else obs.labels
        cur_status = scenario.STATUS_TODO if fresh else obs.status
        cur_blocks_slugs = set() if fresh else _slugs_of_keys(obs.blocks, observed)

        if fresh:
            # Creation sets the *planned* due date; a slip that already applies on
            # this day is a second write, so the changelog shows the move.
            if story.slip and state.day >= story.slip[0]:
                cur_due = _iso(scenario.sim_date(story.due))
        if cur_due != _iso(st.due):
            comment = _slip_comment(story, state.day) if story.slip else None
            actions.append(Action("due", slug, _iso(st.due), comment))
        if cur_start != _iso(st.start):
            actions.append(Action("start", slug, _iso(st.start)))
        if cur_points is None or float(cur_points) != float(st.points):
            actions.append(Action("points", slug, st.points))
        for lb in sorted(st.labels - cur_labels):
            actions.append(Action("label_add", slug, lb))
        if PROGRAM_LABEL in cur_labels and PROGRAM_LABEL not in st.labels:
            actions.append(Action("label_remove", slug, PROGRAM_LABEL))  # the silent break
        for target in st.blocks:
            if target not in cur_blocks_slugs:
                actions.append(Action("link", slug, target))
        if cur_flag != st.flagged:
            comment = _flag_comment(story, state.day, st.flagged)
            actions.append(Action("flag", slug, st.flagged, comment))
        if cur_status != st.status:
            actions.append(
                Action("status", slug, st.status, _transition_comment(story, st, state.day))
            )
    return actions


def _slugs_of_keys(keys: set[str], observed: dict[str, Observed]) -> set[str]:
    by_key = {o.key: slug for slug, o in observed.items()}
    return {by_key[k] for k in keys if k in by_key}


# --- applying ----------------------------------------------------------------------------------


@dataclass
class Report:
    day: int
    actions: list[Action] = field(default_factory=list)
    keys: dict[str, str] = field(default_factory=dict)  # slug -> key (incl. "epic")
    dry_run: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.actions)


def converge(
    jira: Jira,
    day: int,
    *,
    board_id: int,
    dry_run: bool = False,
    log=print,
) -> Report:
    """Make Jira match `state_at(day)`. Returns what was (or would be) done."""
    state = scenario.state_at(day)
    epic = find_epic(jira)
    observed = load(jira, epic.key) if epic else {}
    actions = diff(state, epic, observed)
    report = Report(day=day, actions=actions, dry_run=dry_run)
    keys = {slug: o.key for slug, o in observed.items()}
    if epic:
        keys["epic"] = epic.key

    if not actions:
        log(f"day {day:>2}  {state.date}  in sync — nothing to do")
        report.keys = keys
        return report

    account = None if dry_run else jira.my_account_id()
    for a in actions:
        log(("[dry-run] " if dry_run else "") + str(a))
        if dry_run:
            keys.setdefault(a.slug, f"<{a.slug}>")
            continue
        if a.kind == "create_epic":
            keys["epic"] = jira.create_epic(
                state.epic_summary, sorted(state.epic_labels), project=PROJECT,
                due=_iso(state.epic_due),
            )
        elif a.kind == "epic_due":
            jira.set_fields(keys["epic"], {"duedate": a.value})
        elif a.kind == "epic_label_add":
            jira.add_labels(keys["epic"], [str(a.value)])
        elif a.kind == "epic_label_remove":
            jira.remove_labels(keys["epic"], [str(a.value)])
        elif a.kind == "create":
            st = state.issues[a.slug]
            story = scenario.BY_SLUG[a.slug]
            planned_due = scenario.sim_date(story.due)  # the slip is applied as a later write
            keys[a.slug] = jira.create_story(
                st.summary, sorted(st.labels), due=_iso(planned_due), start=_iso(st.start),
                assignee_id=account, parent=keys["epic"], project=PROJECT,
                description=scenario.description_for(story),
            )
        elif a.kind == "due":
            jira.set_fields(keys[a.slug], {"duedate": a.value})
            if a.comment:
                jira.add_comment(keys[a.slug], a.comment)
        elif a.kind == "start":
            jira.set_fields(keys[a.slug], {START_DATE_FIELD: a.value})
        elif a.kind == "points":
            jira.set_estimation(keys[a.slug], float(a.value), board_id)  # type: ignore[arg-type]
        elif a.kind == "label_add":
            jira.add_labels(keys[a.slug], [str(a.value)])
        elif a.kind == "label_remove":
            jira.remove_labels(keys[a.slug], [str(a.value)])
        elif a.kind == "link":
            jira.create_blocks_link(keys[a.slug], keys[str(a.value)])
        elif a.kind == "flag":
            jira.set_flagged(keys[a.slug], bool(a.value))
            if a.comment:
                jira.add_comment(keys[a.slug], a.comment)
        elif a.kind == "status":
            if jira.transition_to(keys[a.slug], str(a.value)) and a.comment:
                jira.add_comment(keys[a.slug], a.comment)
        else:  # pragma: no cover
            raise ValueError(f"unknown action {a.kind}")
    report.keys = keys
    return report


def verify(jira: Jira, day: int) -> list[Action]:
    """Actions still outstanding for `day` — empty means Jira matches the scenario."""
    state = scenario.state_at(day)
    epic = find_epic(jira)
    observed = load(jira, epic.key) if epic else {}
    return diff(state, epic, observed)


def teardown(jira: Jira, *, dry_run: bool = False, log=print) -> int:
    """Delete every simulated story, then the epic. Returns the number deleted."""
    epic = find_epic(jira)
    if epic is None:
        log("nothing to tear down — no simulated epic found")
        return 0
    observed = load(jira, epic.key)
    count = 0
    for slug, obs in sorted(observed.items()):
        if dry_run:
            log(f"[dry-run] would delete {obs.key} ({slug})")
        else:
            jira.delete_issue(obs.key)  # raises on a permission problem; nothing is logged first
            log(f"deleted {obs.key} ({slug})")
        count += 1
    if dry_run:
        log(f"[dry-run] would delete {epic.key} (epic)")
    else:
        jira.delete_issue(epic.key)
        log(f"deleted {epic.key} (epic)")
    return count + 1
