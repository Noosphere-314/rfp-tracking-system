"""Worker-side alerting.

Pipeline failures inside n8n are reported by its Error Trigger workflow; this
module covers what happens before the webhook: fetch failures, quarantined
config rows, and items that exhausted their delivery attempts (A3, A4).
"""

from __future__ import annotations

import logging

import httpx

from .config import config

log = logging.getLogger(__name__)

_LEVEL_PREFIX = {"info": "ℹ️", "warning": "⚠️", "error": "🚨"}


def alert(message: str, level: str = "warning") -> None:
    """Log, and post to Slack if a webhook is configured.

    Never raises: an alerting failure must not take down the run it is
    reporting on.
    """
    log.log(
        logging.ERROR if level == "error" else logging.WARNING,
        "ALERT[%s] %s",
        level,
        message,
    )
    if not config.slack_webhook_url:
        return
    try:
        httpx.post(
            config.slack_webhook_url,
            json={"text": f"{_LEVEL_PREFIX.get(level, '')} [worker] {message}"},
            timeout=10.0,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        log.warning("could not post alert to Slack: %s", exc)


def ping_healthchecks(suffix: str = "") -> None:
    """Dead-man's switch. Silence here is what tells us the worker stopped."""
    if not config.healthchecks_url:
        return
    url = config.healthchecks_url.rstrip("/") + suffix
    try:
        httpx.get(url, timeout=10.0)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not ping healthchecks.io: %s", exc)
