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

    # Slack (drift/notify.py)
    slack_webhook_url: str | None = None

    # Runtime
    dry_run: bool = True


settings = Settings()
