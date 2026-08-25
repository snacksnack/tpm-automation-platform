"""Central configuration, loaded from environment / .env.

All secrets are optional so the app (and CI) can boot without them — the
health check and import graph must work with no credentials present. Modules
that actually need a value validate it at call time.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Jira (collectors/)
    jira_base_url: str = "https://hirereidcollins.atlassian.net"
    jira_email: str | None = None
    jira_api_token: str | None = None

    # Anthropic (narrative/)
    anthropic_api_key: str | None = None
    # Anthropic Admin API (collectors/billing.py, RC1-308): sk-ant-admin…, a
    # different key class — the regular API key cannot read the cost report.
    anthropic_admin_key: str | None = None

    # Heroku Platform API (collectors/billing.py, RC1-308): reads the account's
    # invoices — the store plan's real bill instead of a declared constant.
    heroku_api_key: str | None = None

    # Slack (drift/notify.py)
    slack_webhook_url: str | None = None

    # Runtime
    dry_run: bool = True
    project_key: str = "RC1"
    db_path: str = "data/drift.db"  # on Fly, point at the mounted volume: /data/drift.db
    # Shared secret to gate POST /drift/run (the GitHub Actions cron sends it as
    # X-Drift-Token). Unset => endpoint is open (local dev only).
    drift_run_token: str | None = None

    # Program simulator (simulate/, RC1-299). Story points are written through the
    # Agile estimation endpoint, which needs the board whose estimation field
    # they are; 68 is the PMA scrum board.
    kpi_sim_board_id: int = 68
    kpi_sim_dir: str = "data/kpi-sim"


settings = Settings()
