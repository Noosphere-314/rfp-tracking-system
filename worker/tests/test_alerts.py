"""Тести worker/alerts.py: алерти в БД + приватний Telegram (2026-08-31).

До цього alert() слав ТІЛЬКИ в Slack-вебхук, який ніколи не був
налаштований — «Filecoin падає в кожному прогоні» тижнями жило лише в
docker logs. Тепер: рядок в alerts (міграція 016) ЗАВЖДИ; пуш у
Telegram-бот — лише коли таке саме message не з'являлось за останні
_TELEGRAM_DEDUP_HOURS (щогодинний повтор того самого провалу не має
стати спамом, від якого бота вимкнуть). Все best-effort: жоден збій
алертингу не валить прогін, про який він звітує.

Без БД і мережі: psycopg.connect і httpx.post — monkeypatch.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

from worker import alerts


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self, dup_row, calls):
        self._dup = dup_row
        self.calls = calls
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "SELECT 1 FROM alerts" in sql:
            return _Cursor([{"?column?": 1}] if self._dup else [])
        return _Cursor([])

    def commit(self):
        self.committed = True


def _wire(monkeypatch, *, dup=False, db_raises=False, token="tok", chat="42"):
    calls: list = []
    posts: list = []

    def connect(url):
        if db_raises:
            raise RuntimeError("db down")
        return _Conn(dup, calls)

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    monkeypatch.setattr(alerts.httpx, "post",
                        lambda url, **kw: posts.append((url, kw)))
    # config — frozen dataclass: підміняємо ЦІЛИЙ об'єкт у модулі alerts,
    # а не поля на живому (FrozenInstanceError).
    monkeypatch.setattr(alerts, "config", SimpleNamespace(
        database_url="postgresql://x", slack_webhook_url="",
        alert_telegram_token=token, alert_telegram_chat_id=chat,
    ))
    return calls, posts


def test_alert_stores_and_pushes_when_fresh(monkeypatch):
    calls, posts = _wire(monkeypatch)
    alerts.alert("Filecoin: GITHUB_TOKEN missing", level="warning")

    assert any("INSERT INTO alerts" in sql for sql, _ in calls)
    assert len(posts) == 1
    url, kw = posts[0]
    assert "api.telegram.org" in url and "/sendMessage" in url
    assert kw["json"]["chat_id"] == "42"
    assert "Filecoin" in kw["json"]["text"]


def test_duplicate_within_window_stores_but_does_not_push(monkeypatch):
    """Щогодинний повтор: історія на /runs повна, пуш тихне."""
    calls, posts = _wire(monkeypatch, dup=True)
    alerts.alert("Filecoin: GITHUB_TOKEN missing")

    assert any("INSERT INTO alerts" in sql for sql, _ in calls)
    assert posts == []


def test_db_failure_still_pushes(monkeypatch):
    """БД лягла — це САМЕ той випадок, коли пуш потрібен."""
    _, posts = _wire(monkeypatch, db_raises=True)
    alerts.alert("worker run crashed: OperationalError", level="error")
    assert len(posts) == 1


def test_no_token_means_no_push_but_still_stored(monkeypatch):
    calls, posts = _wire(monkeypatch, token="")
    alerts.alert("something")
    assert any("INSERT INTO alerts" in sql for sql, _ in calls)
    assert posts == []


def test_telegram_failure_never_raises(monkeypatch):
    calls, _ = _wire(monkeypatch)

    def boom(url, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(alerts.httpx, "post", boom)
    alerts.alert("still fine")  # не має кинути
    assert any("INSERT INTO alerts" in sql for sql, _ in calls)
