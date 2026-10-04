"""Release notes (RC1-497) — offline, no network.

GitHub and Confluence are each a small in-memory fake behind an httpx
MockTransport, so the run filtering, the commit → PR mapping and the page
create/update/unchanged paths are real code paths. The acceptance criteria
that are about behavior — no second message on a re-run, a failed deploy not
reported as shipped, a re-run replacing the day's entry — each have a test
named for them.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from kpi import release_notes as rn

PATH = rn.DeployPath("repo", "svc", "deploy.yml")
REPO = f"/repos/{rn.OWNER}/repo"


def _pr(number: int, branch: str, title: str, merged_at: str | None, base: str = "main") -> dict:
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/{rn.OWNER}/repo/pull/{number}",
        "head": {"ref": branch},
        "base": {"ref": base},
        "merged_at": merged_at,
    }


def _run(run_id: int, sha: str, finished: str) -> dict:
    return {"id": run_id, "head_sha": sha, "updated_at": finished}


def _github(
    *,
    runs: list[dict],
    compare: dict[str, list[str]] | None = None,
    pulls: dict[str, list[dict]] | None = None,
    attempts: dict[tuple[int, int], str] | None = None,
) -> httpx.Client:
    """`runs` is what the API returns for status=success, newest first."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"{REPO}/actions/workflows/deploy.yml/runs":
            assert request.url.params["status"] == "success"
            assert request.url.params["branch"] == "main"
            return httpx.Response(200, json={"workflow_runs": runs})
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
        text = rn.notify(http, PATH, sha="head1234abcd", run_id=11, run_attempt=1)
    first, second = text.splitlines()
    assert (
        first == f"*svc* deployed <https://github.com/{rn.OWNER}/repo/commit/head1234abcd|head1234>"
    )
    assert f"<https://github.com/{rn.OWNER}/repo/pull/104|#104>" in second
    # Slack reads < > & as markup; a title must not be able to open a link.
    assert "pull &lt;field&gt; &amp; more" in second
    assert second.endswith("/browse/RC1-482|RC1-482>)")


def test_a_rerun_of_a_green_run_does_not_post_a_second_message():
    with _github(runs=[], attempts={(11, 1): "success"}) as http:
        assert rn.notify(http, PATH, sha="head", run_id=11, run_attempt=2) is None


def test_a_rerun_after_a_failed_attempt_does_announce():
    pr = _pr(5, "rc1-5-x", "RC1-5: x", "2026-10-01T10:00:00Z")
    with _github(
        runs=[_run(10, "prev", "2026-10-01T09:00:00Z")],
        compare={"prev...head": ["head"]},
        pulls={"head": [pr]},
        attempts={(11, 1): "failure"},
    ) as http:
        assert "#5" in rn.notify(http, PATH, sha="head", run_id=11, run_attempt=2)


def test_the_runs_own_id_is_never_its_own_base():
    # After a re-run the run's id can already be in the success list.
    pr = _pr(5, "rc1-5-x", "RC1-5: x", "2026-10-01T10:00:00Z")
    with _github(
        runs=[_run(11, "head", "2026-10-01T10:05:00Z"), _run(10, "prev", "2026-10-01T09:00:00Z")],
        compare={"prev...head": ["head"]},
        pulls={"head": [pr]},
        attempts={(11, 1): "failure"},
    ) as http:
        assert "#5" in rn.notify(http, PATH, sha="head", run_id=11, run_attempt=2)


def test_a_deploy_with_no_merged_pr_posts_nothing():
    with _github(
        runs=[_run(10, "prev", "2026-10-01T09:00:00Z")], compare={"prev...head": ["head"]}
    ) as http:
        assert rn.notify(http, PATH, sha="head", run_id=11, run_attempt=1) is None


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


def test_a_service_with_no_deploys_that_day_gets_no_entry():
    with _github(runs=[_run(1, "before", "2026-09-30T15:00:00Z")]) as http:
        assert rn.collect_day(http, PATH, DAY) is None


def test_a_merge_whose_deploy_failed_is_not_reported_as_shipped():
    # PR 2 merged on the day but its deploy failed, so its run is not in the
    # success list and the day's range ends at d1. It ships the day a later
    # deploy carries it.
    pr1 = _pr(1, "rc1-1-a", "RC1-1: a", "2026-10-01T15:00:00Z")
    pr2 = _pr(2, "rc1-2-b", "RC1-2: b", "2026-10-01T19:00:00Z")
    with _github(
        runs=[
            _run(4, "d3", "2026-10-02T15:00:00Z"),
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
    url = rn.publish(fake.client(), _entry(DAY, 2))
    assert url == "https://example.atlassian.net/wiki/spaces/RC1/pages/2"
    (_, parent), (_, child) = fake.writes
    assert parent["title"] == rn.PARENT_TITLE and "parentId" not in parent
    assert child["title"] == "Release notes: svc" and child["parentId"] == "1"
    assert child["spaceId"] == "98307"


def test_a_later_day_updates_the_page_with_the_next_version():
    old = rn.render_section(_entry(date(2026, 9, 30), 1))
    fake = FakeWiki({rn.PARENT_TITLE: rn.PARENT_BODY, "Release notes: svc": old})
    assert rn.publish(fake.client(), _entry(DAY, 2)) is not None
    ((method, payload),) = fake.writes
    assert method == "PUT" and payload["version"]["number"] == 2
    assert payload["body"]["value"] == rn.render_section(_entry(DAY, 2)) + old


def test_publishing_the_same_day_twice_writes_once():
    fake = FakeWiki({rn.PARENT_TITLE: rn.PARENT_BODY})
    wiki = fake.client()
    assert rn.publish(wiki, _entry(DAY, 2)) is not None
    writes = len(fake.writes)
    # None is what tells the caller not to post the Slack message again.
    assert rn.publish(wiki, _entry(DAY, 2)) is None
    assert len(fake.writes) == writes


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


def test_notify_posts_once_when_there_is_something_to_say(monkeypatch):
    monkeypatch.setattr(rn, "notify", lambda *a, **k: "msg")
    monkeypatch.setattr(rn.settings, "slack_releases_webhook_url", "https://hooks.example/x")
    posted: list[tuple[str, str]] = []
    monkeypatch.setattr(rn, "post_slack", lambda url, text: posted.append((url, text)))
    argv = ["notify", "--repo", "repo", "--service", "svc", "--workflow", "deploy.yml"]
    assert rn.main([*argv, "--sha", "abc", "--run-id", "1"]) == 0
    assert posted == [("https://hooks.example/x", "msg")]


def test_notify_with_nothing_to_announce_posts_nothing(monkeypatch):
    monkeypatch.setattr(rn, "notify", lambda *a, **k: None)
    monkeypatch.setattr(rn, "post_slack", lambda *a: pytest.fail("must not post"))
    argv = ["notify", "--repo", "repo", "--service", "svc", "--workflow", "deploy.yml"]
    assert rn.main([*argv, "--sha", "abc", "--run-id", "1"]) == 0
