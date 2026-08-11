"""Тести Chat history (розділ «Chat history»): /chats, /chats/view.

Без мережі й без БД: `admin.app.db` завжди підмінений monkeypatch'ем на
об'єкт, що записує SQL+параметри в `sink` (той самий прийом, що й у
test_chat.py/test_briefs.py). Сесія — вже покрита
`test_every_get_route_requires_a_session` у test_auth.py (він ітерує
`app.routes`, тобто підхопить обидва нові маршрути сам, без окремого тесту
тут); CSRF тут не при чому — обидва маршрути звичайні GET, поза роутером
`mutations`.

Імпорт test_auth ПЕРШИМ — env виставляється до першого імпорту admin.app
(той самий трюк, що й в інших тестах admin/tests/)."""

from __future__ import annotations

from datetime import datetime, timezone

from admin.tests.test_auth import SAME_ORIGIN, WHO, _login, client  # noqa: E402,F401
from admin import auth  # noqa: E402


def _csrf(client) -> str:
    return auth.csrf_for(client.cookies[auth.COOKIE_BASE])

from admin import app as admin_app  # noqa: E402


class _Cursor:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Conn:
    def __init__(self, rows: list[dict] | None = None, sink: list | None = None):
        self.rows = rows or []
        self.sink = sink if sink is not None else []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sink.append((sql, params))
        return _Cursor(self.rows)

    def commit(self):
        pass


class _NoDb:
    """Для перевірки, що хендлер узагалі не йде в БД (порожній `key`)."""

    def __call__(self):
        raise AssertionError("хендлер не мав звертатися до БД без key")


def _fake_db(monkeypatch, rows=None, sink=None):
    conn = _Conn(rows, sink)
    monkeypatch.setattr(admin_app, "db", lambda: conn)
    return conn


# ── GET /chats: групування сесій ─────────────────────────────────────


def test_chats_page_groups_sessions_and_renders_columns(client, monkeypatch):
    """Один рядок kb.chat_messages, згорнутий у сесію (той шейп, що видає
    SQL у chats_page), рендериться в усі колонки таблиці."""
    _login(client)
    now = datetime.now(timezone.utc)
    _fake_db(
        monkeypatch,
        rows=[{
            "session_key": "web:abc123", "channel": "web",
            "started": now, "last_at": now,
            "messages": 4, "questions": 2, "tokens": 350,
            "who": WHO, "preview": "which grants exist for oracle work?",
        }],
    )
    html = client.get("/chats").text
    assert "which grants exist for oracle work?" in html
    assert "/chats/view?key=web%3Aabc123" in html
    assert WHO in html
    assert ">2<" in html  # questions
    assert ">350<" in html  # tokens
    assert "Web" in html  # val.channel.web дефолт en


def test_chats_page_renders_telegram_channel_badge(client, monkeypatch):
    _login(client)
    now = datetime.now(timezone.utc)
    _fake_db(
        monkeypatch,
        rows=[{
            "session_key": "telegram:999", "channel": "telegram",
            "started": now, "last_at": now,
            "messages": 1, "questions": 1, "tokens": 0,
            "who": None, "preview": "hi",
        }],
    )
    html = client.get("/chats").text
    assert "Telegram" in html
    assert "b-info" in html
    assert "—" in html  # who відсутній → тире, не порожньо і не KeyError


# ── GET /chats: фільтри змінюють SQL ─────────────────────────────────


def test_chats_page_channel_filter_adds_where_clause(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)

    client.get("/chats", params={"channel": "telegram"})
    sql, params = sink[0]
    assert "channel = %s" in sql
    assert list(params) == ["telegram"]


def test_chats_page_unknown_channel_value_is_ignored(client, monkeypatch):
    """Довільне значення в query-рядку — просто ігнорується (той самий
    інваріант, що й min_confidence на /items): без фільтра, а не 400."""
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)

    client.get("/chats", params={"channel": "carrier-pigeon"})
    sql, params = sink[0]
    assert "channel = %s" not in sql
    assert list(params) == []


def test_chats_page_period_7d_adds_having_clause(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)

    client.get("/chats", params={"period": "7d"})
    sql, _ = sink[0]
    assert "HAVING max(created_at) > now() - interval '7 days'" in sql


def test_chats_page_period_30d_adds_having_clause(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)

    client.get("/chats", params={"period": "30d"})
    sql, _ = sink[0]
    assert "HAVING max(created_at) > now() - interval '30 days'" in sql


def test_chats_page_no_period_means_no_having(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)

    client.get("/chats")
    sql, _ = sink[0]
    assert "HAVING" not in sql


def test_chats_page_channel_and_period_combine(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)

    client.get("/chats", params={"channel": "web", "period": "30d"})
    sql, params = sink[0]
    assert "channel = %s" in sql
    assert "interval '30 days'" in sql
    assert list(params) == ["web"]


# ── GET /chats: порожній стан ─────────────────────────────────────────


def test_chats_page_empty_state_without_filters_links_to_chat(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch, rows=[])

    html = client.get("/chats").text
    assert "No chat history yet" in html
    assert 'href="/chat"' in html


def test_chats_page_empty_state_with_filters_offers_reset(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch, rows=[])

    html = client.get("/chats", params={"channel": "web"}).text
    assert "No sessions match this filter" in html
    assert 'href="/chats"' in html


# ── GET /chats/view: бульбашки й екранування ──────────────────────────


def test_chat_view_renders_bubbles_with_correct_alignment_classes(client, monkeypatch):
    _login(client)
    now = datetime.now(timezone.utc)
    _fake_db(
        monkeypatch,
        rows=[
            {"id": 1, "channel": "web", "role": "user", "who": WHO,
             "content": "hi there", "tier": None, "model": None,
             "tokens_in": None, "tokens_out": None, "created_at": now},
            {"id": 2, "channel": "web", "role": "assistant", "who": None,
             "content": "hello back", "tier": "llm", "model": "claude-x",
             "tokens_in": 100, "tokens_out": 50, "created_at": now},
        ],
    )
    html = client.get("/chats/view", params={"key": "web:abc123"}).text
    assert "chat__list" in html
    assert "chat__msg--user" in html
    assert "chat__msg--assistant" in html
    assert "150" in html  # totals: 100 + 50 tokens


def test_chat_view_escapes_xss_in_content(client, monkeypatch):
    """Рядок з <script> у content має вийти escaped-текстом, не виконуваним
    тегом — той самий контракт, що й chat.html (autoescape Jinja)."""
    _login(client)
    now = datetime.now(timezone.utc)
    payload = "<script>alert(1)</script>"
    _fake_db(
        monkeypatch,
        rows=[
            {"id": 1, "channel": "telegram", "role": "user", "who": "someone",
             "content": payload, "tier": None, "model": None,
             "tokens_in": None, "tokens_out": None, "created_at": now},
        ],
    )
    html = client.get("/chats/view", params={"key": "telegram:1"}).text
    assert payload not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_chat_view_linkifies_assistant_text_but_not_user_text(client, monkeypatch):
    _login(client)
    now = datetime.now(timezone.utc)
    _fake_db(
        monkeypatch,
        rows=[
            {"id": 1, "channel": "web", "role": "user", "who": WHO,
             "content": "see https://example.com", "tier": None, "model": None,
             "tokens_in": None, "tokens_out": None, "created_at": now},
            {"id": 2, "channel": "web", "role": "assistant", "who": None,
             "content": "sure: https://example.com/x", "tier": "llm",
             "model": "claude-x", "tokens_in": 1, "tokens_out": 1,
             "created_at": now},
        ],
    )
    html = client.get("/chats/view", params={"key": "web:abc123"}).text
    assert '<a href="https://example.com/x" target="_blank" rel="noopener">' in html
    assert "see https://example.com</p>" in html


def test_chat_view_shows_stub_chip(client, monkeypatch):
    _login(client)
    now = datetime.now(timezone.utc)
    _fake_db(
        monkeypatch,
        rows=[
            {"id": 1, "channel": "telegram", "role": "assistant", "who": None,
             "content": "keyword-tier reply", "tier": "stub", "model": None,
             "tokens_in": None, "tokens_out": None, "created_at": now},
        ],
    )
    html = client.get("/chats/view", params={"key": "telegram:1"}).text
    assert "keyword tier" in html  # pg.chat.stub_chip, дефолт en


def test_chat_view_header_shows_channel_badge_and_session_key(client, monkeypatch):
    _login(client)
    now = datetime.now(timezone.utc)
    _fake_db(
        monkeypatch,
        rows=[
            {"id": 1, "channel": "telegram", "role": "user", "who": "alice",
             "content": "hi", "tier": None, "model": None,
             "tokens_in": None, "tokens_out": None, "created_at": now},
        ],
    )
    html = client.get("/chats/view", params={"key": "telegram:42"}).text
    assert "Telegram" in html
    assert "telegram:42" in html


# ── GET /chats/view: порожній/невідомий key ────────────────────────────


def test_chat_view_without_key_returns_200_with_empty_state_and_skips_db(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(admin_app, "db", _NoDb())

    response = client.get("/chats/view")
    assert response.status_code == 200
    assert "Session not found" in response.text


def test_chat_view_unknown_key_returns_200_with_empty_state_not_500(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch, rows=[])

    response = client.get("/chats/view", params={"key": "web:does-not-exist"})
    assert response.status_code == 200
    assert "Session not found" in response.text


# ── Видалення розмов (запит Миколи: історія цінна, але прибирається) ──


def test_delete_chat_session_removes_by_exact_key(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)
    response = client.post(
        "/chats/delete",
        data={"key": "telegram:12345", "csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/chats"
    assert any(
        "DELETE FROM kb.chat_messages" in sql and params == ("telegram:12345",)
        for sql, params in sink
    )


def test_delete_chat_session_with_empty_key_touches_nothing(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)
    response = client.post(
        "/chats/delete",
        data={"key": "", "csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert sink == []


def test_prune_chats_deletes_only_older_than_30_days(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)
    response = client.post(
        "/chats/prune",
        data={"csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert any(
        "DELETE FROM kb.chat_messages" in sql and "30 days" in sql
        for sql, _params in sink
    )
