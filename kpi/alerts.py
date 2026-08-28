"""The dead-man's switch: alert when the daily job stops landing readings (RC1-319).

Escalate (RC1-307) raises stale and broken KPIs — but only when the daily
job runs. If launchd never fires (machine asleep at 07:00, broken venv, dead
credential), nothing posts, and silence looks the same as health. This is
the one watcher that does not live on the laptop: a Grafana alert rule that
fires when `kpi_readings` has gone ~36 hours without a single new row —
one missed morning tolerated, two is a page.

    python -m kpi.alerts --out grafana/            # regenerate alerts.json
    python -m kpi.alerts --out grafana/ --push     # …and provision the stack

Provisioned as code for the same reason the dashboards are generated
(RC1-318): hand-configured UI state drifts, and a drift test pins the
committed JSON to this module. The push lands four pieces over the
provisioning API — the `kpi` folder, the Slack contact point, a child
notification route matched on the rule's `channel` label (the root policy is
left untouched), and the rule — all idempotent, so re-running `--push` is
also how a hand-mangled rule gets restored to canon.

What the rule deliberately does NOT watch: staleness, brokenness, exit
codes. A stale reading is still a row — the job ran and recorded honestly,
and escalate owns that story. Only the absence of rows means the pipeline
itself went quiet, which is why the query counts on `computed_at` (the
wall clock of the write) and never `sim_date` (the program's clock).

Secrets stay out of the committed file: the Slack webhook is a
`${SLACK_WEBHOOK_URL}` placeholder resolved at push time from settings, the
datasource a `${DS_POSTGRES}` placeholder resolved by asking the stack —
the same move `kpi.dashboards` makes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

from config import settings
from kpi.dashboards import PushError, _client, _datasource_uid
from kpi.readings_store import TABLE

FOLDER_UID = "kpi"
FOLDER_TITLE = "KPI"
RULE_UID = "kpi-deadman"
RULE_GROUP = "kpi-deadman"
CONTACT_POINT = "kpi-deadman-slack"
CHANNEL_LABEL = ("channel", "kpi-slack")

#: One missed morning is tolerated (the machine was asleep, the next 07:00
#: catches up); a second consecutive miss is the page.
WINDOW_HOURS = 36

#: How often the stack re-evaluates, and how long the condition must hold
#: before firing. The window does the real debouncing — these only need to
#: outlast a transient Postgres blip, not a missed morning.
EVAL_INTERVAL_SECONDS = 1800
FOR = "1h"

_EXPR = {"type": "__expr__", "uid": "__expr__"}


def alert_rule() -> dict:
    sql = (
        f'SELECT count(*) AS landings FROM {TABLE}\n'
        f"WHERE computed_at > now() - interval '{WINDOW_HOURS} hours'"
    )
    return {
        "uid": RULE_UID,
        "title": "KPI daily job went quiet",
        "ruleGroup": RULE_GROUP,
        "folderUID": FOLDER_UID,
        "condition": "C",
        "data": [
            {
                "refId": "A",
                "relativeTimeRange": {"from": 3600, "to": 0},
                "datasourceUid": "${DS_POSTGRES}",
                "model": {
                    "refId": "A",
                    "editorMode": "code",
                    "format": "table",
                    "rawQuery": True,
                    "rawSql": sql,
                    "intervalMs": 1000,
                    "maxDataPoints": 43200,
                },
            },
            {
                "refId": "B",
                "relativeTimeRange": {"from": 0, "to": 0},
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "B",
                    "type": "reduce",
                    "datasource": _EXPR,
                    "expression": "A",
                    "reducer": "last",
                    "settings": {"mode": "dropNN"},
                },
            },
            {
                "refId": "C",
                "relativeTimeRange": {"from": 0, "to": 0},
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "C",
                    "type": "threshold",
                    "datasource": _EXPR,
                    "expression": "B",
                    "conditions": [
                        {
                            "evaluator": {"params": [1], "type": "lt"},
                            "operator": {"type": "and"},
                            "query": {"params": ["C"]},
                            "reducer": {"params": [], "type": "last"},
                            "type": "query",
                        }
                    ],
                },
            },
        ],
        # Both mean "cannot tell whether readings landed", and a store the
        # stack cannot read is one the track stage could not write either.
        "noDataState": "Alerting",
        "execErrState": "Alerting",
        "for": FOR,
        "annotations": {
            "summary": (
                f"No KPI reading has landed in {WINDOW_HOURS} hours. The daily job "
                "itself has gone quiet — launchd not firing, machine asleep two "
                "mornings running, broken venv, or the store unreachable. Escalate "
                "cannot report this: it only runs when the job runs (RC1-319)."
            ),
        },
        "labels": {CHANNEL_LABEL[0]: CHANNEL_LABEL[1]},
        "isPaused": False,
    }


def contact_point() -> dict:
    return {
        "name": CONTACT_POINT,
        "type": "slack",
        "settings": {"url": "${SLACK_WEBHOOK_URL}"},
        "disableResolveMessage": False,
    }


def route() -> dict:
    """The child route appended under the root policy. Matching on the label
    rather than replacing the root receiver leaves whatever else the stack
    routes (nothing today) exactly as it was."""
    return {
        "receiver": CONTACT_POINT,
        "object_matchers": [[CHANNEL_LABEL[0], "=", CHANNEL_LABEL[1]]],
    }


def bundle() -> dict:
    """Everything the push provisions, in one committed, diffable document."""
    return {
        "folder": {"uid": FOLDER_UID, "title": FOLDER_TITLE},
        "contactPoint": contact_point(),
        "route": route(),
        "rule": alert_rule(),
        "evalIntervalSeconds": EVAL_INTERVAL_SECONDS,
    }


def write(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "alerts.json"
    path.write_text(json.dumps(bundle(), indent=2) + "\n")
    return path


# --- push ------------------------------------------------------------------------------------


def _check(resp: httpx.Response, what: str) -> httpx.Response:
    if resp.status_code >= 300:
        raise PushError(f"{what} -> HTTP {resp.status_code}: {resp.text[:200]}")
    return resp


def push(client: httpx.Client, *, webhook_url: str) -> list[str]:
    lines = []

    # Folder: the rule needs a home, and "kpi" is where any later rule goes
    # too. Create-and-accept-conflict rather than check-then-create: on this
    # stack the token can create folders but GET /api/folders/{uid} answers
    # 403 (folders:read is not in its grant), so existence cannot be probed.
    resp = client.post("/api/folders", json={"uid": FOLDER_UID, "title": FOLDER_TITLE})
    if resp.status_code < 300:
        lines.append(f"created folder {FOLDER_UID}")
    elif resp.status_code not in (409, 412):  # both flavors of "already exists"
        raise PushError(f"create folder -> HTTP {resp.status_code}: {resp.text[:200]}")

    # Contact point: upsert by name; the webhook is substituted here and only here.
    cp = json.loads(json.dumps(contact_point()).replace("${SLACK_WEBHOOK_URL}", webhook_url))
    existing = _check(client.get("/api/v1/provisioning/contact-points"),
                      "list contact points").json()
    match = next((c for c in existing if c.get("name") == CONTACT_POINT), None)
    if match:
        _check(client.put(f"/api/v1/provisioning/contact-points/{match['uid']}", json=cp),
               "update contact point")
    else:
        _check(client.post("/api/v1/provisioning/contact-points", json=cp),
               "create contact point")
    lines.append(f"contact point {CONTACT_POINT} -> Slack webhook")

    # Notification route: append our child route unless one already targets
    # the contact point. The rest of the tree goes back byte-for-byte.
    tree = _check(client.get("/api/v1/provisioning/policies"), "read policy tree").json()
    routes = tree.setdefault("routes", [])
    if not any(r.get("receiver") == CONTACT_POINT for r in routes):
        routes.append(route())
        _check(client.put("/api/v1/provisioning/policies", json=tree), "update policy tree")
        lines.append(f"routed {CHANNEL_LABEL[0]}={CHANNEL_LABEL[1]} -> {CONTACT_POINT}")

    # The rule: upsert by uid, datasource resolved the way dashboards resolve it.
    uid = _datasource_uid(client)
    rule = json.loads(json.dumps(alert_rule()).replace("${DS_POSTGRES}", uid))
    if client.get(f"/api/v1/provisioning/alert-rules/{RULE_UID}").status_code == 404:
        _check(client.post("/api/v1/provisioning/alert-rules", json=rule), "create alert rule")
    else:
        _check(client.put(f"/api/v1/provisioning/alert-rules/{RULE_UID}", json=rule),
               "update alert rule")

    # Evaluation cadence lives on the rule group, not the rule.
    group = _check(client.get(f"/api/v1/provisioning/folder/{FOLDER_UID}/rule-groups/{RULE_GROUP}"),
                   "read rule group").json()
    if group.get("interval") != EVAL_INTERVAL_SECONDS:
        group["interval"] = EVAL_INTERVAL_SECONDS
        _check(client.put(f"/api/v1/provisioning/folder/{FOLDER_UID}/rule-groups/{RULE_GROUP}",
                          json=group), "set rule group interval")
    lines.append(
        f"rule {RULE_UID}: no row in {WINDOW_HOURS}h -> alert "
        f"(eval {EVAL_INTERVAL_SECONDS // 60}m, for {FOR})"
    )
    return lines


# --- CLI -------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m kpi.alerts",
        description="Generate (and with --push, provision) the dead-man's-switch alert.",
    )
    ap.add_argument("--out", type=Path, default=Path("grafana"), help="directory to write into")
    ap.add_argument(
        "--push", action="store_true",
        help="after writing, provision the alert on the Grafana stack "
             "(settings.grafana_url; GRAFANA_TOKEN in ~/.zshrc)",
    )
    args = ap.parse_args(argv)

    token = os.environ.get("GRAFANA_TOKEN")
    if args.push and not token:
        print(
            "GRAFANA_TOKEN is not set (it lives in ~/.zshrc, next to EVAL_DATABASE_URL); "
            "nothing written, nothing pushed",
            file=sys.stderr,
        )
        return 2
    if args.push and not settings.slack_webhook_url:
        print("SLACK_WEBHOOK_URL is not set; the alert would fire into the void",
              file=sys.stderr)
        return 2

    print(f"wrote {write(args.out)}")

    if args.push:
        try:
            with _client(settings.grafana_url, token) as client:
                for line in push(client, webhook_url=settings.slack_webhook_url):
                    print(line)
        except PushError as exc:
            print(f"push failed: {exc}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
