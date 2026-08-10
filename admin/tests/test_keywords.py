"""Тести AI-помічника ключових слів (розділ A): POST /keywords/advice.

Без мережі й без БД: `admin.app._keywords_advice_backend` завжди підмінена
monkeypatch'ем (той самий прийом, що й `_chat_backend` у test_chat.py), а
`admin.app.db` — фейковим conn, що повертає порожній список keywords (сам
список рядків тут не в фокусі — фокус на тому, що робить хендлер з
advice_md/error у контексті шаблону).

Сесія й CSRF-покриття `/keywords/advice` — під загальними тестами в
test_auth.py (роут-ітерація по `app.routes`), тут не дублюються.

Імпорт test_auth ПЕРШИМ — env виставляється до першого імпорту admin.app
(той самий трюк, що й в інших тестах admin/tests/)."""

from __future__ import annotations

import httpx

from admin.tests.test_auth import SAME_ORIGIN, _login, client  # noqa: E402,F401

from admin import app as admin_app  # noqa: E402
from admin import auth  # noqa: E402


class _Cursor:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Conn:
    def __init__(self, rows: list[dict] | None = None):
        self.rows = rows or []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        return _Cursor(self.rows)

    def commit(self):
        pass


def _fake_db(monkeypatch, rows=None):
    monkeypatch.setattr(admin_app, "db", lambda: _Conn(rows))


def _csrf(client) -> str:
    return auth.csrf_for(client.cookies[auth.COOKIE_BASE])


# ── POST /keywords/advice: PRG навмисно зламаний ──────────────────────


def test_keywords_advice_happy_path_renders_advice_md_via_md_lite(client, monkeypatch):
    """Успіх рендерить ТУ САМУ сторінку /keywords напряму з POST (без 303) —
    advice_md проходить через md_lite (той самий рендерер, що й brief.html),
    тож markdown із kbmcp стає справжніми тегами, не сирим текстом."""
    _login(client)
    _fake_db(monkeypatch, rows=[])
    monkeypatch.setattr(
        admin_app,
        "_keywords_advice_backend",
        lambda: {
            "ok": True,
            "advice_md": "## Try this\n- add **hackathon**",
            "model": "claude-x",
        },
    )
    response = client.post(
        "/keywords/advice",
        data={"csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 200  # НЕ 303 — PRG навмисно зламаний тут
    html = response.text
    assert "<h3>Try this</h3>" in html
    assert "<strong>hackathon</strong>" in html
    assert "claude-x" in html


def test_keywords_advice_ok_false_shows_inline_error(client, monkeypatch):
    """kbmcp відповів 503 {"ok": false, "error": ...} — сторінка показує
    бейлінгвальне повідомлення (t() з англійським дефолтом), а не сирий
    текст помилки з kbmcp напряму."""
    _login(client)
    _fake_db(monkeypatch, rows=[])
    monkeypatch.setattr(
        admin_app,
        "_keywords_advice_backend",
        lambda: {"ok": False, "error": "rate limited, try later"},
    )
    response = client.post(
        "/keywords/advice",
        data={"csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 503
    assert "Could not generate keyword suggestions right now" in response.text


def test_keywords_advice_unreachable_backend_shows_inline_error(client, monkeypatch):
    """Мережа впала (kbmcp недоступний) — httpx.HTTPError, не 500."""
    _login(client)
    _fake_db(monkeypatch, rows=[])

    def boom():
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(admin_app, "_keywords_advice_backend", boom)
    response = client.post(
        "/keywords/advice",
        data={"csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 502
    assert "Could not reach the AI advice backend" in response.text


def test_keywords_page_has_no_advice_panel_before_any_click(client, monkeypatch):
    """GET /keywords (без advice_md у контексті) не повинен рендерити панель
    порад узагалі — вона з'являється лише як прямий наслідок POST."""
    _login(client)
    _fake_db(monkeypatch, rows=[])
    html = client.get("/keywords").text
    assert 'id="advice"' not in html
