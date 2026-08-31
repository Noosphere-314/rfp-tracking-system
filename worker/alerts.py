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


# Дедуп для Telegram: те саме повідомлення не летить у бот частіше, ніж раз
# на стільки годин. Filecoin без токена падає КОЖЕН щогодинний прогін —
# 24 однакові приватні повідомлення на день змусили б вимкнути бота, і
# алерти знову ніхто б не бачив. У БД при цьому пишеться КОЖЕН випадок:
# історія на /runs повна, тихне лише пуш.
_TELEGRAM_DEDUP_HOURS = 6


def _store(message: str, level: str) -> bool:
    """Рядок в alerts (міграція 016) → чи слати в Telegram (дедуп).

    ВЛАСНЕ коротке з'єднання, а не conn викликача — свідомо: алерти летять
    і з crash-хендлера run_once, де основне з'єднання вже може бути в
    зламаному стані; алерт про смерть прогону не має залежати від здоров'я
    того, про що він повідомляє. Never raises."""
    import psycopg

    try:
        with psycopg.connect(config.database_url) as conn:
            dup = conn.execute(
                "SELECT 1 FROM alerts WHERE message = %s "
                "AND created_at > now() - make_interval(hours => %s) LIMIT 1",
                (message, _TELEGRAM_DEDUP_HOURS),
            ).fetchone()
            conn.execute(
                "INSERT INTO alerts (level, message) VALUES (%s, %s)",
                (level, message),
            )
            conn.commit()
        return dup is None
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        log.warning("could not store alert in DB: %s", exc)
        # БД лягла — це саме той випадок, коли пуш ПОТРІБЕН: шли завжди.
        return True


def alert(message: str, level: str = "warning") -> None:
    """Log + рядок у БД (видимий на /runs) + приватне повідомлення в
    Telegram-бот (рішення Миколи 2026-08-31: бот, НЕ група; Slack-гілка
    лишається для сумісності, але вебхук ніколи не був налаштований).

    Never raises: an alerting failure must not take down the run it is
    reporting on.
    """
    log.log(
        logging.ERROR if level == "error" else logging.WARNING,
        "ALERT[%s] %s",
        level,
        message,
    )
    fresh = _store(message, level)

    if fresh and config.alert_telegram_token and config.alert_telegram_chat_id:
        try:
            httpx.post(
                f"https://api.telegram.org/bot{config.alert_telegram_token}/sendMessage",
                json={
                    "chat_id": config.alert_telegram_chat_id,
                    "text": f"{_LEVEL_PREFIX.get(level, '')} [worker] {message}",
                },
                timeout=10.0,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort by design
            log.warning("could not post alert to Telegram: %s", exc)

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
