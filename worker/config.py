"""Environment-backed configuration.

The worker holds source-side read credentials only. Write-side keys (Pipedrive,
Claude, Slack posting from the pipeline) live in n8n credentials — one owner per
key, so there is no rotation drift (System-Design.md §9).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw else default


@dataclass(frozen=True)
class Config:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL"))

    n8n_webhook_url: str = field(default_factory=lambda: _env("N8N_WEBHOOK_URL"))
    n8n_webhook_secret: str = field(default_factory=lambda: _env("N8N_WEBHOOK_SECRET"))

    run_interval_seconds: int = field(
        default_factory=lambda: _int("RUN_INTERVAL_SECONDS", 3600)
    )
    # How far back a fetcher looks when a source has no watermark yet, and the
    # overlap it keeps afterwards — forums edit and bump, so a strict watermark
    # would miss late-arriving posts.
    fetch_lookback_hours: int = field(
        default_factory=lambda: _int("FETCH_LOOKBACK_HOURS", 72)
    )
    pending_retry_after_minutes: int = field(
        default_factory=lambda: _int("PENDING_RETRY_AFTER_MINUTES", 120)
    )
    max_delivery_attempts: int = field(
        default_factory=lambda: _int("MAX_DELIVERY_ATTEMPTS", 5)
    )

    host_rate_limit_rps: float = field(
        default_factory=lambda: _float("HOST_RATE_LIMIT_RPS", 1.0)
    )
    host_rate_limit_burst: float = field(
        default_factory=lambda: _float("HOST_RATE_LIMIT_BURST", 5.0)
    )
    source_stagger_seconds: float = field(
        default_factory=lambda: _float("SOURCE_STAGGER_SECONDS", 2.0)
    )
    http_timeout_seconds: float = field(
        default_factory=lambda: _float("HTTP_TIMEOUT_SECONDS", 30.0)
    )
    user_agent: str = field(
        default_factory=lambda: _env(
            "USER_AGENT", "RFP-Tracker/1.0 (+mailto:ops@example.com)"
        )
    )
    items_log_retention_days: int = field(
        default_factory=lambda: _int("ITEMS_LOG_RETENTION_DAYS", 90)
    )
    # Regex evaluation budget per pattern per text (seconds). A team-editable
    # pattern must not be able to hang the run.
    regex_timeout_seconds: float = field(
        default_factory=lambda: _float("REGEX_TIMEOUT_SECONDS", 0.25)
    )

    snapshot_api_key: str = field(default_factory=lambda: _env("SNAPSHOT_API_KEY"))
    github_token: str = field(default_factory=lambda: _env("GITHUB_TOKEN"))
    cryptorank_api_key: str = field(default_factory=lambda: _env("CRYPTORANK_API_KEY"))

    healthchecks_url: str = field(default_factory=lambda: _env("HEALTHCHECKS_URL"))
    slack_webhook_url: str = field(default_factory=lambda: _env("SLACK_WEBHOOK_URL"))

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("DATABASE_URL", self.database_url),
                ("N8N_WEBHOOK_URL", self.n8n_webhook_url),
                ("N8N_WEBHOOK_SECRET", self.n8n_webhook_secret),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"missing required environment: {', '.join(missing)}")


config = Config()
