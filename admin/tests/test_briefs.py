"""Тести /briefs — lifecycle (архів/розархів/видалення/завантаження, розділ
4.8, розширення). Без БД і без мережі: `admin.app.db` завжди підмінений
monkeypatch'ем на об'єкт, що записує SQL+параметри в `sink`. Сесія й CSRF —
під загальними тестами в test_auth.py (роут-ітерація), тут — лише поведінка
самих хендлерів.

Імпорт test_auth ПЕРШИМ — env виставляється до першого імпорту admin.app
(той самий трюк, що й в інших тестах admin/tests/)."""

from __future__ import annotations

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


def _fake_db(monkeypatch, rows=None, sink=None):
    conn = _Conn(rows, sink)
    monkeypatch.setattr(admin_app, "db", lambda: conn)
    return conn


def _csrf(client) -> str:
    return auth.csrf_for(client.cookies[auth.COOKIE_BASE])


# ── GET /briefs: заархівовані ховаються за замовчуванням ────────────────


def test_briefs_default_hides_archived(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)

    response = client.get("/briefs")
    assert response.status_code == 200
    sql, _ = sink[0]
    assert "archived_at IS NULL" in sql


def test_briefs_archived_1_shows_everything(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)

    response = client.get("/briefs", params={"archived": "1"})
    assert response.status_code == 200
    sql, _ = sink[0]
    assert "archived_at IS NULL" not in sql


def test_briefs_ecosystem_and_tier_combine_with_the_archived_default(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)

    client.get("/briefs", params={"ecosystem": "Optimism", "tier": "llm"})
    sql, params = sink[0]
    assert "archived_at IS NULL" in sql
    assert "ecosystem = %s" in sql and "tier = %s" in sql
    assert list(params) == ["Optimism", "llm"]


# ── POST /briefs/{id}/archive · /unarchive · /delete ─────────────────────


def test_archive_brief_sets_archived_at_only_if_not_already_archived(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, sink=sink)

    response = client.post(
        "/briefs/7/archive",
        data={"csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/briefs/7"
    sql, params = sink[0]
    assert "archived_at = now()" in sql
    assert "archived_at IS NULL" in sql  # ідемпотентність: не чіпає вже архівний
    assert params == (7,)


def test_unarchive_brief_clears_archived_at(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, sink=sink)

    response = client.post(
        "/briefs/7/unarchive",
        data={"csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/briefs/7"
    sql, params = sink[0]
    assert "archived_at = NULL" in sql
    assert params == (7,)


def test_delete_brief_removes_the_row_and_redirects_to_the_list(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, sink=sink)

    response = client.post(
        "/briefs/7/delete",
        data={"csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/briefs"
    sql, params = sink[0]
    assert sql.strip().startswith("DELETE FROM kb.briefs")
    assert params == (7,)


# ── GET /briefs/{id}/download ─────────────────────────────────────────────


def test_download_brief_returns_markdown_attachment(client, monkeypatch):
    _login(client)
    _fake_db(
        monkeypatch,
        rows=[{"title": "Optimism grants Q3", "brief_md": "### Overview\nhello"}],
    )

    response = client.get("/briefs/7/download")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert 'attachment; filename="Optimism-grants-Q3.md"' in response.headers["content-disposition"]
    assert response.text == "### Overview\nhello"


def test_download_brief_sanitizes_unsafe_title_characters(client, monkeypatch):
    _login(client)
    _fake_db(
        monkeypatch,
        rows=[{"title": 'weird "title"/with:chars', "brief_md": "x"}],
    )
    response = client.get("/briefs/7/download")
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert '"' not in disposition.split("filename=", 1)[1][1:-1]  # усередині лапок filename — нема лапок
    assert "/" not in disposition.split("filename=")[1]


def test_download_brief_404_when_missing(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch, rows=[])
    response = client.get("/briefs/999/download")
    assert response.status_code == 404


# ── /share/briefs/{id}: magic-лінк read-only перегляду (2026-08-31) ───────


def _share_token(brief_id: int, secret: str = "share-secret", offset: int = 3600) -> str:
    import hashlib
    import hmac as hmac_mod
    import time as time_mod

    expiry = int(time_mod.time()) + offset
    sig = hmac_mod.new(secret.encode(), f"{brief_id}|{expiry}".encode(),
                       hashlib.sha256).hexdigest()[:32]
    return f"{expiry}.{sig}"


_BRIEF_ROW = {
    "id": 7, "item_uid": None, "ecosystem": "EVM",
    "title": "Weekly grants & RFPs — 2026-08-31",
    "brief_md": "## New this week\n- Something concrete",
    "tier": "llm", "model": "claude-opus-5", "tokens_in": 1, "tokens_out": 1,
    "created_at": None, "archived_at": None,
}


def test_share_brief_opens_without_a_session(client, monkeypatch):
    """Головний сенс magic-лінка: СЕО тисне лінк із Telegram і читає звіт
    БЕЗ логін-стіни — /share/ у auth.PUBLIC_PREFIX, автентифікація = HMAC."""
    monkeypatch.setenv("KB_MCP_TOKEN", "share-secret")
    _fake_db(monkeypatch, rows=[_BRIEF_ROW])

    r = client.get(f"/share/briefs/7?t={_share_token(7)}")
    assert r.status_code == 200
    assert "Something concrete" in r.text
    # Документ, не адмінка: без сайдбара і без дій життєвого циклу.
    assert "sidebar" not in r.text
    assert "/briefs/7/delete" not in r.text
    assert r.headers["cache-control"] == "private, no-store"


def test_share_brief_rejects_bad_expired_and_foreign_tokens(client, monkeypatch):
    monkeypatch.setenv("KB_MCP_TOKEN", "share-secret")
    _fake_db(monkeypatch, rows=[_BRIEF_ROW])

    assert client.get("/share/briefs/7").status_code == 404
    assert client.get("/share/briefs/7?t=garbage").status_code == 404
    # Прострочений.
    assert client.get(f"/share/briefs/7?t={_share_token(7, offset=-10)}").status_code == 404
    # Підпис від ІНШОГО brief_id.
    assert client.get(f"/share/briefs/7?t={_share_token(8)}").status_code == 404
    # Чужий секрет.
    assert client.get(f"/share/briefs/7?t={_share_token(7, secret='wrong')}").status_code == 404


def test_share_brief_fails_closed_without_the_secret(client, monkeypatch):
    """Порожній KB_MCP_TOKEN → жоден токен не валідний (інакше hmac з
    порожнім ключем був би ПІДРОБЛЮВАНИМ будь-ким, хто знає формат)."""
    monkeypatch.delenv("KB_MCP_TOKEN", raising=False)
    _fake_db(monkeypatch, rows=[_BRIEF_ROW])
    assert client.get(f"/share/briefs/7?t={_share_token(7, secret='')}").status_code == 404


def test_unauthenticated_briefs_link_with_token_redirects_to_share(client, monkeypatch):
    """Перше TG-повідомлення 2026-08-31 пішло з /briefs/{id}?t=… (воркфлоу
    оновили до /share/ пізніше) — старий лінк без сесії веде на
    share-вигляд, а не на логін-стіну."""
    client.cookies.clear()
    r = client.get("/briefs/11?t=123.abc", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/share/briefs/11?t=123.abc"


def test_unauthenticated_briefs_link_without_token_still_hits_login(client):
    client.cookies.clear()
    r = client.get("/briefs/11", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]
