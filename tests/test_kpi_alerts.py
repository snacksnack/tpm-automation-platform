"""The dead-man's-switch alert (RC1-319).

What matters here is the semantics the ticket pinned: the rule watches the
wall clock of writes (never the sim clock), a stale reading still counts as
a landing (it is a row), no secret reaches the committed file, and the push
is idempotent so re-running it restores canon.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from kpi import alerts

GRAFANA = Path(__file__).resolve().parent.parent / "grafana"


def test_the_rule_counts_rows_on_the_wall_clock():
    """`computed_at` is when the job wrote; `sim_date` is the program's clock.
    Watching the wrong one would page on a simulator quirk, not a dead job."""
    sql = alerts.alert_rule()["data"][0]["model"]["rawSql"]
    assert "computed_at" in sql
    assert "sim_date" not in sql


def test_a_stale_reading_still_counts_as_a_landing():
    """The job recording `stale` honestly is a working pipeline — escalate's
    story, not this alert's. Only absence of rows may fire it."""
    sql = alerts.alert_rule()["data"][0]["model"]["rawSql"]
    assert "state" not in sql
    assert sql.startswith("SELECT count(*)")


def test_the_alert_fires_on_zero_rows():
    rule = alerts.alert_rule()
    threshold = rule["data"][2]["model"]["conditions"][0]["evaluator"]
    assert threshold == {"params": [1], "type": "lt"}
    assert rule["condition"] == "C"


def test_cannot_tell_means_alerting():
    """No-data and a query error both mean 'cannot confirm a landing', and a
    store the stack cannot read is one the track stage could not write."""
    rule = alerts.alert_rule()
    assert rule["noDataState"] == "Alerting"
    assert rule["execErrState"] == "Alerting"


def test_no_secret_reaches_the_committed_file():
    dumped = json.dumps(alerts.bundle())
    assert "${SLACK_WEBHOOK_URL}" in dumped
    assert "${DS_POSTGRES}" in dumped
    assert "hooks.slack.com" not in dumped


def test_the_route_matches_the_rule_label():
    rule, route = alerts.alert_rule(), alerts.route()
    key, _, value = route["object_matchers"][0]
    assert rule["labels"].get(key) == value
    assert route["receiver"] == alerts.CONTACT_POINT


def test_the_committed_file_matches_the_generator(tmp_path):
    fresh = json.loads(alerts.write(tmp_path).read_text())
    committed = json.loads((GRAFANA / "alerts.json").read_text())
    assert fresh == committed, "grafana/alerts.json is stale — rerun python -m kpi.alerts"


# --- push ------------------------------------------------------------------------------------


_DS = [{"type": "grafana-postgresql-datasource", "uid": "real-uid", "name": "reid-eval-store"}]


class _Stack:
    """A mock stack that records provisioning writes and can start populated,
    so the same class proves both the first push and idempotent re-push."""

    def __init__(self, *, populated: bool = False):
        self.writes: list[tuple[str, str]] = []  # (method, path)
        self.tree: dict = {"receiver": "empty", "group_by": ["grafana_folder", "alertname"]}
        self.contact_points: list[dict] = []
        self.have_folder = self.have_rule = False
        if populated:
            self.have_folder = self.have_rule = True
            self.contact_points = [{"uid": "cp1", "name": alerts.CONTACT_POINT}]
            self.tree["routes"] = [dict(alerts.route())]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method != "GET":
            self.writes.append((method, path))
        if path == "/api/datasources":
            return httpx.Response(200, json=_DS)
        if path == "/api/folders":
            # The live stack answers a repeat create with 409; GET-by-uid is
            # 403 there, so the code never probes, it creates and accepts.
            if self.have_folder:
                return httpx.Response(409, json={"message": "already exists"})
            self.have_folder = True
            return httpx.Response(200, json={})
        if path == "/api/v1/provisioning/contact-points":
            if method == "POST":
                self.contact_points.append({"uid": "cp1", "name": alerts.CONTACT_POINT})
                return httpx.Response(202, json={})
            return httpx.Response(200, json=self.contact_points)
        if path.startswith("/api/v1/provisioning/contact-points/"):
            return httpx.Response(202, json={})
        if path == "/api/v1/provisioning/policies":
            if method == "PUT":
                self.tree = json.loads(request.content)
                return httpx.Response(202, json={})
            return httpx.Response(200, json=self.tree)
        if path == f"/api/v1/provisioning/alert-rules/{alerts.RULE_UID}":
            if method == "PUT":
                return httpx.Response(200, json={})
            return httpx.Response(200 if self.have_rule else 404, json={})
        if path == "/api/v1/provisioning/alert-rules":
            self.have_rule = True
            self.last_rule = json.loads(request.content)
            return httpx.Response(201, json={})
        if "rule-groups" in path:
            if method == "PUT":
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"title": alerts.RULE_GROUP, "interval": 60,
                                             "rules": []})
        raise AssertionError(f"unexpected call: {method} {path}")

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler),
                            base_url="https://stack.test")


def test_first_push_provisions_all_four_pieces():
    stack = _Stack()
    with stack.client() as client:
        alerts.push(client, webhook_url="https://hooks.example/secret")
    assert ("POST", "/api/folders") in stack.writes
    assert ("POST", "/api/v1/provisioning/contact-points") in stack.writes
    assert ("PUT", "/api/v1/provisioning/policies") in stack.writes
    assert ("POST", "/api/v1/provisioning/alert-rules") in stack.writes


def test_push_resolves_both_placeholders():
    stack = _Stack()
    with stack.client() as client:
        alerts.push(client, webhook_url="https://hooks.example/secret")
    dumped = json.dumps(stack.last_rule)
    assert "${DS_POSTGRES}" not in dumped and '"real-uid"' in dumped
    tree_route = stack.tree["routes"][0]
    assert tree_route["receiver"] == alerts.CONTACT_POINT


def test_push_leaves_the_rest_of_the_policy_tree_alone():
    stack = _Stack()
    with stack.client() as client:
        alerts.push(client, webhook_url="https://hooks.example/secret")
    assert stack.tree["receiver"] == "empty"
    assert stack.tree["group_by"] == ["grafana_folder", "alertname"]


def test_repush_updates_in_place_and_never_duplicates_the_route():
    stack = _Stack(populated=True)
    with stack.client() as client:
        alerts.push(client, webhook_url="https://hooks.example/secret")
    methods = {path: method for method, path in stack.writes}
    assert methods.get("/api/v1/provisioning/contact-points/cp1") == "PUT"
    assert methods.get(f"/api/v1/provisioning/alert-rules/{alerts.RULE_UID}") == "PUT"
    assert ("PUT", "/api/v1/provisioning/policies") not in stack.writes
    assert len(stack.tree["routes"]) == 1


def test_push_without_a_webhook_stops_up_front(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GRAFANA_TOKEN", "t")
    monkeypatch.setattr(alerts.settings, "slack_webhook_url", None)
    assert alerts.main(["--out", str(tmp_path / "g"), "--push"]) == 2
    assert not (tmp_path / "g").exists()
    assert "SLACK_WEBHOOK_URL" in capsys.readouterr().err
