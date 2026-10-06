"""Release notes (RC1-497, RC1-502) — offline, no network.

GitHub and Confluence are each a small in-memory fake behind an httpx
MockTransport, so the run filtering, the commit → PR mapping and the page
create/update/unchanged paths are real code paths. The acceptance criteria
that are about behavior — no second message on a re-run, a failed deploy not
reported as shipped, a re-run replacing the day's entry — each have a test
named for them. The model is a fake client: the summary tests are about when
it is called and what happens when it fails, not about its prose.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace

import httpx
import pytest

from kpi import release_notes as rn

PATH = rn.DeployPath("repo", "svc", "deploy.yml")
REPO = f"/repos/{rn.OWNER}/repo"


@pytest.fixture(autouse=True)
def _no_live_credentials(monkeypatch):
    """A developer's `.env` holds real keys. No test may reach Confluence,
    Slack or the model with them."""
    for name in ("jira_email", "jira_api_token", "anthropic_api_key", "slack_releases_webhook_url"):
        monkeypatch.setattr(rn.settings, name, None)


def _announce(http: httpx.Client, path: rn.DeployPath, *, sha: str, **run) -> str | None:
    """The #releases message for a deploy, or None when it carried nothing."""
    prs = rn.deploy_prs(http, path, sha=sha, **run)
    return rn.render_deploy_message(path, sha, prs) if prs else None


def _pr(number: int, branch: str, title: str, merged_at: str | None, base: str = "main") -> dict:
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/{rn.OWNER}/repo/pull/{number}",
        "head": {"ref": branch},
        "base": {"ref": base},
        "merged_at": merged_at,
    }


def _run(
    run_id: int, sha: str, finished: str, branch: str = "main", conclusion: str = "success"
) -> dict:
    return {
        "id": run_id,
        "head_sha": sha,
        "head_branch": branch,
        "updated_at": finished,
        "conclusion": conclusion,
    }


def _github(
    *,
    runs: list[dict],
    compare: dict[str, list[str]] | None = None,
    pulls: dict[str, list[dict]] | None = None,
    attempts: dict[tuple[int, int], str] | None = None,
    tags: bool = False,
) -> httpx.Client:
    """`runs` is the workflow's whole run list, newest first."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"{REPO}/actions/workflows/deploy.yml/runs":
            # Filtered here, not by the API, so repeated reads compare.
            assert "status" not in request.url.params
            # A tag-released path must not be filtered to the default branch.
            assert request.url.params.get("branch") == (None if tags else "main")
            return httpx.Response(200, json={"total_count": len(runs), "workflow_runs": runs})
        if path.startswith(f"{REPO}/compare/"):
            shas = (compare or {})[path.removeprefix(f"{REPO}/compare/")]
            return httpx.Response(200, json={"commits": [{"sha": s} for s in shas]})
        if path.startswith(f"{REPO}/commits/") and path.endswith("/pulls"):
            sha = path.removeprefix(f"{REPO}/commits/").removesuffix("/pulls")
            return httpx.Response(200, json=(pulls or {}).get(sha, []))
        if path.startswith(f"{REPO}/actions/runs/"):
            _, run_id, _, n = path.removeprefix(f"{REPO}/actions/").split("/")
            return httpx.Response(200, json={"conclusion": (attempts or {})[(int(run_id), int(n))]})
        return httpx.Response(404, json={"message": "Not Found"})

    return httpx.Client(base_url=rn.GITHUB_API, transport=httpx.MockTransport(handler))


# --- story_key -------------------------------------------------------------------------------


def test_the_story_comes_from_the_branch_name():
    assert rn.story_key("rc1-497-release-notes", "whatever") == "RC1-497"


def test_the_title_is_the_fallback_when_the_branch_breaks_the_convention():
    assert rn.story_key("fix-typo", "RC1-12: fix a typo") == "RC1-12"
    assert rn.story_key("dependabot/pip/httpx-0.28", "Bump httpx") is None


# --- shipped_prs -----------------------------------------------------------------------------


def test_commits_map_to_their_prs_once_each_oldest_merge_first():
    pr1 = _pr(1, "rc1-1-a", "RC1-1: a", "2026-10-01T10:00:00Z")
    pr2 = _pr(2, "rc1-2-b", "RC1-2: b", "2026-10-01T12:00:00Z")
    # Two commits belong to PR 2; the later-merged PR's commit is listed first.
    with _github(
        runs=[],
        compare={"base...head": ["c2a", "c2b", "c1"]},
        pulls={"c2a": [pr2], "c2b": [pr2], "c1": [pr1]},
    ) as http:
        prs = rn.shipped_prs(http, "repo", "base", "head")
    assert [p.number for p in prs] == [1, 2]
    assert prs[0].story == "RC1-1"


def test_an_unmerged_pr_or_one_into_another_branch_did_not_ship():
    with _github(
        runs=[],
        compare={"base...head": ["c1"]},
        pulls={
            "c1": [
                _pr(1, "rc1-1-a", "open", None),
                _pr(2, "rc1-2-b", "stacked", "2026-10-01T10:00:00Z", base="rc1-1-a"),
            ]
        },
    ) as http:
        assert rn.shipped_prs(http, "repo", "base", "head") == []


def test_a_redeploy_of_the_same_sha_ships_nothing():
    with _github(runs=[]) as http:  # any request would 404 and raise
        assert rn.shipped_prs(http, "repo", "same", "same") == []


def test_the_first_recorded_deploy_reads_only_its_own_commit():
    pr = _pr(1, "rc1-1-a", "RC1-1: a", "2026-10-01T10:00:00Z")
    with _github(runs=[], pulls={"head": [pr]}) as http:
        assert [p.number for p in rn.shipped_prs(http, "repo", None, "head")] == [1]


# --- notify ----------------------------------------------------------------------------------


def test_the_message_names_the_service_links_the_pr_and_the_story():
    pr = _pr(104, "rc1-482-x", "RC1-482: pull <field> & more", "2026-10-01T19:18:52Z")
    with _github(
        runs=[_run(10, "prev", "2026-10-01T19:00:00Z")],
        compare={"prev...head1234abcd": ["head1234abcd"]},
        pulls={"head1234abcd": [pr]},
    ) as http:
        text = _announce(http, PATH, sha="head1234abcd", run_id=11, run_attempt=1)
    first, second = text.splitlines()
    assert (
        first == f"*svc* deployed <https://github.com/{rn.OWNER}/repo/commit/head1234abcd|head1234>"
    )
    assert f"<https://github.com/{rn.OWNER}/repo/pull/104|#104>" in second
    # Slack reads < > & as markup; a title must not be able to open a link.
    assert "pull &lt;field&gt; &amp; more" in second
    assert second.endswith("/browse/RC1-482|RC1-482>)")


def test_the_message_links_the_release_notes_page_when_it_was_written():
    pr = rn.PullRequest(1, "t", "https://github.com/x/y/pull/1", None)
    first = rn.render_deploy_message(PATH, "head1234abcd", [pr], "https://w/p/2").splitlines()[0]
    assert first.endswith("|head1234> · <https://w/p/2|release notes>")


def test_a_rerun_of_a_green_run_does_not_post_a_second_message():
    with _github(runs=[], attempts={(11, 1): "success"}) as http:
        assert _announce(http, PATH, sha="head", run_id=11, run_attempt=2) is None


def test_a_rerun_after_a_failed_attempt_does_announce():
    pr = _pr(5, "rc1-5-x", "RC1-5: x", "2026-10-01T10:00:00Z")
    with _github(
        runs=[_run(10, "prev", "2026-10-01T09:00:00Z")],
        compare={"prev...head": ["head"]},
        pulls={"head": [pr]},
        attempts={(11, 1): "failure"},
    ) as http:
        assert "#5" in _announce(http, PATH, sha="head", run_id=11, run_attempt=2)


def test_the_runs_own_id_is_never_its_own_base():
    # After a re-run the run's id can already be in the success list.
    pr = _pr(5, "rc1-5-x", "RC1-5: x", "2026-10-01T10:00:00Z")
    with _github(
        runs=[_run(11, "head", "2026-10-01T10:05:00Z"), _run(10, "prev", "2026-10-01T09:00:00Z")],
        compare={"prev...head": ["head"]},
        pulls={"head": [pr]},
        attempts={(11, 1): "failure"},
    ) as http:
        assert "#5" in _announce(http, PATH, sha="head", run_id=11, run_attempt=2)


def test_a_deploy_with_no_merged_pr_posts_nothing():
    with _github(
        runs=[_run(10, "prev", "2026-10-01T09:00:00Z")], compare={"prev...head": ["head"]}
    ) as http:
        assert _announce(http, PATH, sha="head", run_id=11, run_attempt=1) is None


def test_a_stale_run_list_loses_to_a_longer_read():
    # GitHub sometimes answers with a snapshot that ends weeks ago. One such
    # read among several must not make a deploy day look like a quiet one.
    fresh = [_run(2, "new", "2026-10-01T15:00:00Z"), _run(1, "old", "2026-09-17T15:00:00Z")]
    answers = iter([fresh[1:], fresh, fresh[1:]])

    def handler(request: httpx.Request) -> httpx.Response:
        runs = next(answers)
        return httpx.Response(200, json={"total_count": len(runs), "workflow_runs": runs})

    with httpx.Client(base_url=rn.GITHUB_API, transport=httpx.MockTransport(handler)) as http:
        assert [r.sha for r in rn.successful_runs(http, PATH)] == ["new", "old"]
    assert next(answers, None) is None  # exactly RUN_LIST_READS reads


# --- a tag-released path ---------------------------------------------------------------------

TAGGED = rn.DeployPath("repo", "lib", "deploy.yml", tags=True)


def test_a_release_is_ranged_and_named_by_tag_not_by_sha():
    # The run's head_sha is deliberately useless here: for an annotated tag it
    # can be the tag object, so the tag name is what gets compared and linked.
    pr = _pr(40, "rc1-9-x", "RC1-9: x", "2026-10-01T10:00:00Z")
    with _github(
        runs=[_run(10, "tagobj1", "2026-09-22T20:00:00Z", branch="v0.6.2")],
        compare={"v0.6.2...v0.6.3": ["c1"]},
        pulls={"c1": [pr]},
        tags=True,
    ) as http:
        text = _announce(http, TAGGED, sha="v0.6.3", run_id=11, run_attempt=1)
    first, second = text.splitlines()
    assert (
        first == f"*lib* released <https://github.com/{rn.OWNER}/repo/releases/tag/v0.6.3|v0.6.3>"
    )
    assert "#40" in second


def test_a_release_day_entry_links_the_tag():
    pr = _pr(40, "rc1-9-x", "RC1-9: x", "2026-10-01T10:00:00Z")
    with _github(
        runs=[
            _run(11, "tagobj2", "2026-10-01T15:00:00Z", branch="v0.6.3"),
            _run(10, "tagobj1", "2026-09-22T20:00:00Z", branch="v0.6.2"),
        ],
        compare={"v0.6.2...v0.6.3": ["c1"]},
        pulls={"c1": [pr]},
        tags=True,
    ) as http:
        entry = rn.collect_day(http, TAGGED, date(2026, 10, 1))
    assert entry.head_sha == "v0.6.3"
    assert '<p>1 release, ending at <a href="https://github.com/' in rn.render_section(entry)
    assert '/releases/tag/v0.6.3">v0.6.3</a>' in rn.render_section(entry)


def test_notify_uses_the_known_path_for_a_repo_the_digest_covers(monkeypatch):
    seen: list[rn.DeployPath] = []
    monkeypatch.setattr(rn, "deploy_prs", lambda http, path, **k: seen.append(path) or [])
    argv = ["notify", "--repo", "agent-evals", "--service", "agent-evals"]
    assert rn.main([*argv, "--workflow", "release.yml", "--sha", "v1", "--run-id", "1"]) == 0
    assert seen[0].tags is True


# --- collect_day -----------------------------------------------------------------------------

DAY = date(2026, 10, 1)


def test_a_day_runs_from_the_last_deploy_before_it_to_the_last_deploy_in_it():
    pr1 = _pr(1, "rc1-1-a", "RC1-1: a", "2026-10-01T15:00:00Z")
    pr2 = _pr(2, "rc1-2-b", "RC1-2: b", "2026-10-01T19:00:00Z")
    with _github(
        runs=[
            _run(4, "next-day", "2026-10-02T15:00:00Z"),
            _run(3, "d2", "2026-10-01T19:05:00Z"),
            _run(2, "d1", "2026-10-01T15:05:00Z"),
            _run(1, "before", "2026-09-30T15:00:00Z"),
        ],
        compare={"before...d2": ["d1", "d2"]},
        pulls={"d1": [pr1], "d2": [pr2]},
    ) as http:
        entry = rn.collect_day(http, PATH, DAY)
    assert entry.deploys == 2
    assert entry.head_sha == "d2"
    assert [p.number for p in entry.prs] == [1, 2]


def test_a_day_is_a_new_york_day_not_a_utc_day():
    # 02:00 UTC on 10-02 is 22:00 on 10-01 in New York: it belongs to 10-01.
    pr = _pr(1, "rc1-1-a", "RC1-1: a", "2026-10-02T01:55:00Z")
    with _github(
        runs=[_run(2, "late", "2026-10-02T02:00:00Z"), _run(1, "before", "2026-09-30T15:00:00Z")],
        compare={"before...late": ["late"]},
        pulls={"late": [pr]},
    ) as http:
        assert rn.collect_day(http, PATH, DAY).head_sha == "late"
        assert rn.collect_day(http, PATH, date(2026, 10, 2)) is None


def test_the_deploy_in_progress_counts_as_the_days_newest():
    # Called from inside its own run, a deploy is not in the run list yet. The
    # day's entry must still end at it and cover the earlier deploy's PR too.
    pr1 = _pr(1, "rc1-1-a", "RC1-1: a", "2026-10-01T15:00:00Z")
    pr2 = _pr(2, "rc1-2-b", "RC1-2: b", "2026-10-01T18:00:00Z")
    now = datetime(2026, 10, 1, 18, 5, tzinfo=UTC)
    with _github(
        runs=[_run(2, "d1", "2026-10-01T15:05:00Z"), _run(1, "d0", "2026-09-30T15:00:00Z")],
        compare={"d0...d2": ["c1", "c2"]},
        pulls={"c1": [pr1], "c2": [pr2]},
    ) as http:
        entry = rn.collect_day(http, PATH, DAY, current=rn.Run(3, "d2", now))
    assert (entry.deploys, entry.head_sha) == (2, "d2")
    assert [p.number for p in entry.prs] == [1, 2]


def test_a_service_with_no_deploys_that_day_gets_no_entry():
    with _github(runs=[_run(1, "before", "2026-09-30T15:00:00Z")]) as http:
        assert rn.collect_day(http, PATH, DAY) is None


def test_a_day_whose_deploys_carried_no_merged_pr_gets_no_entry():
    with _github(
        runs=[_run(2, "d1", "2026-10-01T15:05:00Z"), _run(1, "before", "2026-09-30T15:00:00Z")],
        compare={"before...d1": ["d1"]},
    ) as http:
        assert rn.collect_day(http, PATH, DAY) is None


def test_a_merge_whose_deploy_failed_is_not_reported_as_shipped():
    # PR 2 merged on the day but its deploy failed (and one was skipped), so
    # the day's range ends at d1. It ships the day a later deploy carries it.
    pr1 = _pr(1, "rc1-1-a", "RC1-1: a", "2026-10-01T15:00:00Z")
    pr2 = _pr(2, "rc1-2-b", "RC1-2: b", "2026-10-01T19:00:00Z")
    with _github(
        runs=[
            _run(4, "d3", "2026-10-02T15:00:00Z"),
            _run(3, "d2", "2026-10-01T19:05:00Z", conclusion="failure"),
            _run(5, "d2", "2026-10-01T19:01:00Z", conclusion="skipped"),
            _run(2, "d1", "2026-10-01T15:05:00Z"),
            _run(1, "before", "2026-09-30T15:00:00Z"),
        ],
        compare={"before...d1": ["d1"], "d1...d3": ["d2", "d3"]},
        pulls={"d1": [pr1], "d2": [pr2]},
    ) as http:
        assert [p.number for p in rn.collect_day(http, PATH, DAY).prs] == [1]
        assert [p.number for p in rn.collect_day(http, PATH, date(2026, 10, 2)).prs] == [2]


# --- merge_day -------------------------------------------------------------------------------


def _entry(day: date, *numbers: int) -> rn.DayEntry:
    prs = tuple(
        rn.PullRequest(n, f"RC1-{n}: <t>", f"https://github.com/x/y/pull/{n}", f"RC1-{n}")
        for n in numbers
    )
    return rn.DayEntry(path=PATH, day=day, deploys=len(numbers), head_sha="abcdef0123", prs=prs)


def test_a_section_escapes_the_title_and_links_pr_and_story():
    section = rn.render_section(_entry(DAY, 7))
    assert section.startswith("<h2>2026-10-01</h2><p>1 deploy, ending at ")
    assert '<a href="https://github.com/x/y/pull/7">#7</a> RC1-7: &lt;t&gt;' in section
    assert '/browse/RC1-7">RC1-7</a>' in section


def test_a_new_day_goes_above_older_days_and_the_preamble_stays():
    older = rn.render_section(_entry(date(2026, 9, 30), 1))
    newer = rn.render_section(_entry(DAY, 2))
    assert rn.merge_day("<p>intro</p>" + older, DAY, newer) == "<p>intro</p>" + newer + older


def test_a_backfilled_day_lands_in_date_order():
    d1, d2, d3 = (rn.render_section(_entry(date(2026, 10, n), n)) for n in (1, 2, 3))
    assert rn.merge_day(d3 + d1, date(2026, 10, 2), d2) == d3 + d2 + d1


def test_rerunning_a_day_replaces_its_entry_instead_of_adding_a_second():
    other = rn.render_section(_entry(date(2026, 9, 30), 1))
    first = rn.render_section(_entry(DAY, 2))
    second = rn.render_section(_entry(DAY, 2, 3))
    merged = rn.merge_day(first + other, DAY, second)
    assert merged == second + other
    assert merged.count("<h2>2026-10-01</h2>") == 1


def test_a_heading_confluence_decorated_is_still_found():
    stored = '<h2 local-id="ab12">2026-10-01</h2><p>old</p>'
    new = rn.render_section(_entry(DAY, 2))
    assert rn.merge_day(stored, DAY, new) == new


# --- publish ---------------------------------------------------------------------------------


class FakeWiki:
    """Confluence v2, as much of it as the digest touches."""

    def __init__(self, pages: dict[str, str] | None = None):
        self.pages = {
            title: {"id": str(i), "title": title, "body": body, "version": 1}
            for i, (title, body) in enumerate((pages or {}).items(), start=1)
        }
        self.writes: list[tuple[str, dict]] = []

    def _page(self, p: dict) -> dict:
        return {
            "id": p["id"],
            "title": p["title"],
            "version": {"number": p["version"]},
            "body": {"storage": {"value": p["body"]}},
            "_links": {"webui": f"/spaces/RC1/pages/{p['id']}"},
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/wiki/api/v2")
        if request.method == "GET" and path == "/spaces":
            return httpx.Response(200, json={"results": [{"id": "98307"}]})
        if request.method == "GET" and path == "/pages":
            page = self.pages.get(request.url.params["title"])
            return httpx.Response(200, json={"results": [self._page(page)] if page else []})
        payload = json.loads(request.content)
        self.writes.append((request.method, payload))
        if request.method == "POST" and path == "/pages":
            page = {
                "id": str(len(self.pages) + 1),
                "title": payload["title"],
                "body": payload["body"]["value"],
                "version": 1,
            }
            self.pages[page["title"]] = page
            return httpx.Response(200, json=self._page(page))
        if request.method == "PUT":
            page = next(p for p in self.pages.values() if p["id"] == path.rsplit("/", 1)[1])
            assert payload["version"]["number"] == page["version"] + 1
            page["body"], page["version"] = payload["body"]["value"], payload["version"]["number"]
            return httpx.Response(200, json=self._page(page))
        return httpx.Response(404)

    def client(self) -> rn.Confluence:
        http = httpx.Client(transport=httpx.MockTransport(self.handler))
        return rn.Confluence(http, "https://example.atlassian.net")


def test_the_first_entry_creates_the_parent_and_the_service_page_under_it():
    fake = FakeWiki()
    published = rn.publish(fake.client(), _entry(DAY, 2))
    assert published.url == "https://example.atlassian.net/wiki/spaces/RC1/pages/2"
    assert published.changed
    (_, parent), (_, child) = fake.writes
    assert parent["title"] == rn.PARENT_TITLE and "parentId" not in parent
    assert child["title"] == "Release notes: svc" and child["parentId"] == "1"
    assert child["spaceId"] == "98307"


def test_a_later_day_updates_the_page_with_the_next_version():
    old = rn.render_section(_entry(date(2026, 9, 30), 1))
    fake = FakeWiki({rn.PARENT_TITLE: rn.PARENT_BODY, "Release notes: svc": old})
    assert rn.publish(fake.client(), _entry(DAY, 2)).changed
    ((method, payload),) = fake.writes
    assert method == "PUT" and payload["version"]["number"] == 2
    assert payload["body"]["value"] == rn.render_section(_entry(DAY, 2)) + old


def test_publishing_the_same_day_twice_writes_once():
    fake = FakeWiki({rn.PARENT_TITLE: rn.PARENT_BODY})
    wiki = fake.client()
    first = rn.publish(wiki, _entry(DAY, 2))
    writes = len(fake.writes)
    # Unchanged is what tells the sweep not to post its Slack message again.
    again = rn.publish(wiki, _entry(DAY, 2))
    assert (first.changed, again.changed) == (True, False)
    assert again.url == first.url  # a deploy still needs the link
    assert len(fake.writes) == writes


# --- the summary -----------------------------------------------------------------------------


class FakeModel:
    """`client.messages.create`, answering with one structured-output block."""

    def __init__(self, summary: str = "Plain words.", points: tuple[str, ...] = (), fail=None):
        self.calls: list[dict] = []
        self.messages = self
        self._answer = json.dumps({"summary": summary, "points": list(points)})
        self._fail = fail

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail:
            raise self._fail
        block = SimpleNamespace(type="text", text=self._answer)
        return SimpleNamespace(stop_reason="end_turn", content=[block])

    def summarize(self, entry: rn.DayEntry) -> rn.Summary | None:
        return rn.summarize(self, entry)


def test_the_model_is_handed_the_days_prs_and_nothing_else():
    pr = rn.PullRequest(7, "RC1-7: t", "https://github.com/x/y/pull/7", "RC1-7", body="why " * 5)
    entry = rn.DayEntry(path=PATH, day=DAY, deploys=1, head_sha="abc", prs=(pr,))
    model = FakeModel(points=("One.", " "))
    assert rn.summarize(model, entry) == rn.Summary("Plain words.", ("One.",))
    (call,) = model.calls
    assert call["model"] == rn.SUMMARY_MODEL
    payload = json.loads(call["messages"][0]["content"])
    assert payload["service"] == "svc" and payload["day"] == "2026-10-01"
    assert payload["pull_requests"] == [
        {
            "number": 7,
            "title": "RC1-7: t",
            "story": "RC1-7",
            "description": "why " * 5,
            "description_truncated": False,
        }
    ]


def test_a_long_description_is_cut_and_the_payload_says_so():
    pr = rn.PullRequest(7, "t", "u", None, body="x" * (rn.BODY_CHARS + 1))
    entry = rn.DayEntry(path=PATH, day=DAY, deploys=1, head_sha="abc", prs=(pr,))
    (sent,) = rn.summary_payload(entry)["pull_requests"]
    assert len(sent["description"]) == rn.BODY_CHARS and sent["description_truncated"] is True


@pytest.mark.parametrize(
    "model",
    [
        FakeModel(fail=RuntimeError("529 overloaded")),
        FakeModel(summary="   "),
    ],
)
def test_a_model_that_fails_or_says_nothing_gives_no_summary_and_a_warning(model, capsys):
    assert rn.summarize(model, _entry(DAY, 2)) is None
    assert "::warning title=Release notes: no summary for svc::" in capsys.readouterr().out


def test_a_summary_is_escaped_and_sits_between_the_date_and_the_facts():
    html = rn.render_summary(rn.Summary("a <b> & c", ('it\'s "quoted"',)))
    assert html == ('<p>a &lt;b&gt; &amp; c</p><ul><li>it\'s "quoted"</li></ul>' + rn.SUMMARY_NOTE)
    section = rn.render_section(_entry(DAY, 7), html)
    assert section == "<h2>2026-10-01</h2>" + html + rn.render_facts(_entry(DAY, 7))


def test_the_entry_opens_with_the_summary_and_keeps_the_pr_list():
    fake, model = FakeWiki({rn.PARENT_TITLE: rn.PARENT_BODY}), FakeModel()
    published = rn.publish(fake.client(), _entry(DAY, 2), model.summarize)
    assert published.summary == "new"
    body = fake.pages["Release notes: svc"]["body"]
    assert body.startswith("<h2>2026-10-01</h2><p>Plain words.</p>" + rn.SUMMARY_NOTE)
    assert body.endswith(rn.render_facts(_entry(DAY, 2)))


def test_a_second_deploy_the_same_day_rewrites_the_one_section_with_a_new_summary():
    fake, model = FakeWiki({rn.PARENT_TITLE: rn.PARENT_BODY}), FakeModel()
    wiki = fake.client()
    rn.publish(wiki, _entry(DAY, 2), model.summarize)
    published = rn.publish(wiki, _entry(DAY, 2, 3), model.summarize)
    assert (published.changed, published.summary) == (True, "new")
    assert len(model.calls) == 2  # the PR set changed, so the old words do not stand
    body = fake.pages["Release notes: svc"]["body"]
    assert body.count("<h2>") == 1 and "#3</a>" in body


def test_the_sweep_leaves_a_current_page_alone_without_a_model_call():
    fake, model = FakeWiki({rn.PARENT_TITLE: rn.PARENT_BODY}), FakeModel()
    wiki = fake.client()
    rn.publish(wiki, _entry(DAY, 2, 3), model.summarize)
    writes, version = len(fake.writes), fake.pages["Release notes: svc"]["version"]
    again = rn.publish(wiki, _entry(DAY, 2, 3), model.summarize)
    assert (again.changed, again.summary) == (False, "kept")
    assert len(model.calls) == 1 and len(fake.writes) == writes
    assert fake.pages["Release notes: svc"]["version"] == version


def test_a_changed_deploy_count_rewrites_the_facts_but_keeps_the_summary():
    # A redeploy with no new PR moves the count and the sha, not the PR set.
    fake, model = FakeWiki({rn.PARENT_TITLE: rn.PARENT_BODY}), FakeModel()
    wiki = fake.client()
    rn.publish(wiki, _entry(DAY, 2), model.summarize)
    more = rn.DayEntry(PATH, DAY, deploys=2, head_sha="fedcba9876", prs=_entry(DAY, 2).prs)
    published = rn.publish(wiki, more, model.summarize)
    assert (published.changed, published.summary) == (True, "kept")
    assert len(model.calls) == 1
    assert "<p>Plain words.</p>" in fake.pages["Release notes: svc"]["body"]


def test_a_summary_confluence_decorated_is_still_found():
    note = "<em>Summary written by AI from the pull requests below.</em>"
    kept = f'<p local-id="a">Plain words.</p><p local-id="b">{note}</p>'
    body = '<h2 local-id="c">2026-10-01</h2>' + kept + rn.render_facts(_entry(DAY, 2))
    assert rn.kept_summary(body, _entry(DAY, 2)) == kept
    assert rn.kept_summary(body, _entry(DAY, 2, 3)) == ""
    assert rn.kept_summary(body, _entry(date(2026, 10, 2), 2)) == ""


def test_a_failed_model_call_still_writes_the_pr_list():
    fake = FakeWiki({rn.PARENT_TITLE: rn.PARENT_BODY})
    model = FakeModel(fail=RuntimeError("down"))
    published = rn.publish(fake.client(), _entry(DAY, 2), model.summarize)
    assert (published.changed, published.summary) == (True, "none")
    assert fake.pages["Release notes: svc"]["body"] == rn.render_section(_entry(DAY, 2))


def test_a_stale_summary_is_dropped_when_the_model_cannot_write_the_new_one():
    fake = FakeWiki({rn.PARENT_TITLE: rn.PARENT_BODY})
    wiki = fake.client()
    rn.publish(wiki, _entry(DAY, 2), FakeModel().summarize)
    rn.publish(wiki, _entry(DAY, 2, 3), FakeModel(fail=RuntimeError("down")).summarize)
    assert fake.pages["Release notes: svc"]["body"] == rn.render_section(_entry(DAY, 2, 3))


def test_a_missing_key_gives_no_summary_and_says_so_only_when_one_was_needed(capsys):
    summarize = rn.summarizer()
    assert capsys.readouterr().out == ""
    assert summarize(_entry(DAY, 2)) is None
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().out


def test_a_missing_space_is_an_error_not_an_empty_page():
    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"results": []}))
    )
    with pytest.raises(LookupError):
        rn.publish(rn.Confluence(http, "https://example.atlassian.net"), _entry(DAY, 2))


def test_the_digest_message_links_each_page_that_changed():
    text = rn.render_digest_message(DAY, [(_entry(DAY, 2, 3), "https://w/p/2")])
    assert text == "Release notes for 2026-10-01\n• <https://w/p/2|svc>: 2 PRs"


# --- main ------------------------------------------------------------------------------------


def test_digest_dry_run_writes_nothing(monkeypatch, capsys):
    monkeypatch.setattr(rn, "collect_day", lambda http, path, day: _entry(day, 2))
    monkeypatch.setattr(rn, "publish", lambda *a: pytest.fail("dry run must not write"))
    monkeypatch.setattr(rn, "post_slack", lambda *a: pytest.fail("dry run must not post"))
    assert rn.main(["digest", "--date", "2026-10-01", "--dry-run"]) == 0
    assert "<h2>2026-10-01</h2>" in capsys.readouterr().out


def test_digest_without_confluence_credentials_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(rn, "collect_day", lambda http, path, day: None)
    monkeypatch.setattr(rn.settings, "jira_email", None)
    assert rn.main(["digest", "--date", "2026-10-01"]) == 2
    assert "JIRA_EMAIL" in capsys.readouterr().err


def test_a_repo_that_cannot_be_read_turns_the_run_red(monkeypatch, capsys):
    def boom(http, path, day):
        raise httpx.ConnectError("down", request=httpx.Request("GET", "https://api.github.com/x"))

    monkeypatch.setattr(rn, "collect_day", boom)
    assert rn.main(["digest", "--date", "2026-10-01", "--dry-run"]) == 1
    assert "::error title=Release notes: tpm-drift-detector::" in capsys.readouterr().out


NOTIFY = ["notify", "--repo", "repo", "--service", "svc", "--workflow", "deploy.yml"]
NOTIFY += ["--sha", "abcdef0123", "--run-id", "1"]


def _a_deploy(monkeypatch) -> list[tuple[str, str]]:
    """One deploy carrying PR #2, Slack configured; returns what gets posted."""
    monkeypatch.setattr(rn, "deploy_prs", lambda *a, **k: list(_entry(DAY, 2).prs))
    monkeypatch.setattr(rn, "collect_day", lambda http, path, day, current: _entry(day, 2))
    monkeypatch.setattr(rn.settings, "slack_releases_webhook_url", "https://hooks.example/x")
    posted: list[tuple[str, str]] = []
    monkeypatch.setattr(rn, "post_slack", lambda url, text: posted.append((url, text)))
    return posted


def test_notify_writes_the_page_first_then_posts_a_message_that_links_it(monkeypatch):
    posted = _a_deploy(monkeypatch)
    monkeypatch.setattr(rn.settings, "jira_email", "me@example.com")
    monkeypatch.setattr(rn.settings, "jira_api_token", "t")
    order: list[str] = []

    def publish(wiki, entry, summarize):
        order.append("page")
        return rn.Published("https://w/p/2", True, "new")

    monkeypatch.setattr(rn, "publish", publish)
    monkeypatch.setattr(
        rn, "post_slack", lambda url, text: order.append("slack") or posted.append(text)
    )
    assert rn.main(NOTIFY) == 0
    assert order == ["page", "slack"]
    assert "<https://w/p/2|release notes>" in posted[0]


def test_notify_without_confluence_credentials_still_posts_without_a_link(monkeypatch, capsys):
    posted = _a_deploy(monkeypatch)
    monkeypatch.setattr(rn, "publish", lambda *a: pytest.fail("no credentials, no write"))
    assert rn.main(NOTIFY) == 0
    ((_, text),) = posted
    assert "release notes>" not in text and "#2" in text
    assert "::warning title=Release notes: page not written::" in capsys.readouterr().out


def test_notify_still_posts_when_the_page_write_fails(monkeypatch, capsys):
    posted = _a_deploy(monkeypatch)
    monkeypatch.setattr(rn.settings, "jira_email", "me@example.com")
    monkeypatch.setattr(rn.settings, "jira_api_token", "t")

    def boom(*a):
        raise httpx.ConnectError("down", request=httpx.Request("GET", "https://x.example/wiki"))

    monkeypatch.setattr(rn, "publish", boom)
    assert rn.main(NOTIFY) == 0
    assert len(posted) == 1 and "release notes>" not in posted[0][1]
    assert "The daily sweep will record this deploy." in capsys.readouterr().out


def test_notify_dry_run_writes_and_posts_nothing(monkeypatch, capsys):
    _a_deploy(monkeypatch)
    monkeypatch.setattr(rn.settings, "jira_email", "me@example.com")
    monkeypatch.setattr(rn.settings, "jira_api_token", "t")
    monkeypatch.setattr(rn, "publish", lambda *a: pytest.fail("dry run must not write"))
    monkeypatch.setattr(rn, "post_slack", lambda *a: pytest.fail("dry run must not post"))
    assert rn.main([*NOTIFY, "--dry-run"]) == 0
    assert "<h2>" in capsys.readouterr().out


def test_notify_with_nothing_to_announce_writes_and_posts_nothing(monkeypatch):
    monkeypatch.setattr(rn, "deploy_prs", lambda *a, **k: [])
    monkeypatch.setattr(rn, "collect_day", lambda *a, **k: pytest.fail("must not collect"))
    monkeypatch.setattr(rn, "post_slack", lambda *a: pytest.fail("must not post"))
    assert rn.main(NOTIFY) == 0


def test_the_sweep_announces_only_the_pages_it_changed(monkeypatch, capsys):
    monkeypatch.setattr(rn, "collect_day", lambda http, path, day: _entry(day, 2))
    monkeypatch.setattr(rn.settings, "jira_email", "me@example.com")
    monkeypatch.setattr(rn.settings, "jira_api_token", "t")
    monkeypatch.setattr(rn.settings, "slack_releases_webhook_url", "https://hooks.example/x")
    results = iter([rn.Published("https://w/p/1", True, "new")])
    monkeypatch.setattr(
        rn, "publish", lambda *a: next(results, rn.Published("https://w/p/9", False, "kept"))
    )
    posted: list[str] = []
    monkeypatch.setattr(rn, "post_slack", lambda url, text: posted.append(text))
    assert rn.main(["digest", "--date", "2026-10-01"]) == 0
    assert posted == ["Release notes for 2026-10-01\n• <https://w/p/1|svc>: 1 PR"]
    assert capsys.readouterr().out.count("already current") == len(rn.PATHS) - 1
