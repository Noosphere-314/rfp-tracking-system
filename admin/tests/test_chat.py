"""Тести AI-чату (розділ 4.9, задача #30): /chat, /chat/send, /chat/new.

Без мережі й без БД: `admin.app._chat_backend` і `admin.app.db` тут завжди
підмінені monkeypatch'ем. `/chat` вимагає сесію — це вже ловить
`test_every_get_route_requires_a_session` у test_auth.py (він ітерує
`app.routes`, тобто підхопить маршрут сам, без окремого тесту тут); так само
CSRF-покриття `/chat/send` і `/chat/new` ловить
`test_every_post_route_is_csrf_covered` — обидва на роутері `mutations`.

Імпорт test_auth, а не копія його env-бутстрапу і `_login`/`client`: модуль
один на процес pytest, тож env виставляється рівно один раз, а логін-флоу не
дублюється (WHO, SAME_ORIGIN — звідти ж).
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

# Імпортувати ПЕРШИМ: test_auth виставляє env (DASHBOARD_PASSWORD,
# SESSION_SECRET, …) ДО того, як сам імпортує admin.app — auth.py читає їх
# на імпорті й падає fail-fast, якщо пароль не заданий. Якщо `admin.app`
# імпортувати тут раніше за цей рядок, той самий файл, запущений одиночно
# (`pytest admin/tests/test_chat.py`), впаде на колекції ще до першого тесту.
from admin.tests.test_auth import SAME_ORIGIN, WHO, _login, client  # noqa: E402,F401

from admin import app as admin_app  # noqa: E402
from admin import auth  # noqa: E402


def _csrf(client) -> str:
    return auth.csrf_for(client.cookies[auth.COOKIE_BASE])


class _Cursor:
    """Мінімальний psycopg-подібний курсор: fetchall/fetchone над готовим
    списком рядків, і запис останнього SQL+параметрів для перевірки
    namespacing (розділ 2 задачі: 'web:' + sid, той самий, що пише kbmcp)."""

    def __init__(self, rows: list[dict], sink: list):
        self.rows = rows
        self.sink = sink

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Conn:
    """Перший execute() у запиті бачить `rows` (сумісність з тестами, писаними
    до розділу D4: там був рівно один SELECT на /chat). GET /chat відтоді
    робить ДРУГИЙ SELECT — форумні чипи (kb.forums) — тож кожен наступний
    виклик за замовчуванням отримує порожній список, а не ті самі `rows`:
    інакше рядки повідомлень (id/role/content/…) підставлялися б замість
    форумних (forum_slug) і падали б KeyError. `extra_rows` дозволяє новим
    тестам задати результат саме для другого (і далі) виклику за індексом."""

    def __init__(
        self,
        rows: list[dict] | None = None,
        sink: list | None = None,
        extra_rows: dict[int, list[dict]] | None = None,
    ):
        self.rows = rows or []
        self.sink = sink if sink is not None else []
        self.extra_rows = extra_rows or {}
        self._call_index = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sink.append((sql, params))
        idx = self._call_index
        self._call_index += 1
        result_rows = self.rows if idx == 0 else self.extra_rows.get(idx, [])
        return _Cursor(result_rows, self.sink)

    def commit(self):
        pass


def _fake_db(monkeypatch, rows=None, sink=None, extra_rows=None):
    conn = _Conn(rows, sink, extra_rows)
    monkeypatch.setattr(admin_app, "db", lambda: conn)
    return conn


# ── GET /chat: історія й «thinking» ─────────────────────────────────


def test_chat_page_reads_web_namespaced_session_key(client, monkeypatch):
    """GET /chat має читати kb.chat_messages під ТИМ САМИМ ключем, під яким
    kbmcp пише (розділ 4.9: 'web:' + sid) — інакше щойно надіслане
    повідомлення просто не знайдеться в історії."""
    _login(client)
    sid = auth.session_sid(
        type("R", (), {"cookies": {auth.COOKIE_BASE: client.cookies[auth.COOKIE_BASE]}})()
    )
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)

    response = client.get("/chat")
    assert response.status_code == 200
    assert sink, "хендлер жодного разу не звернувся до БД"
    sql, params = sink[0]
    assert "kb.chat_messages" in sql
    assert params == (f"web:{sid}",)


def test_chat_page_shows_thinking_hint_when_last_row_is_user(client, monkeypatch):
    """Останній рядок історії — user без відповіді: могла піти в іншу
    вкладку/запит, що досі виконується (POST /chat/send блокується до
    300 с) — сторінка показує натяк, а не мовчить."""
    _login(client)
    now = datetime.now(timezone.utc)
    _fake_db(
        monkeypatch,
        rows=[
            {"id": 1, "role": "user", "who": WHO, "content": "hi",
             "tier": None, "model": None, "created_at": now},
        ],
    )
    response = client.get("/chat")
    assert response.status_code == 200
    assert "chat__msg--thinking" in response.text


def test_chat_page_no_thinking_hint_when_answered(client, monkeypatch):
    _login(client)
    now = datetime.now(timezone.utc)
    _fake_db(
        monkeypatch,
        rows=[
            {"id": 1, "role": "user", "who": WHO, "content": "hi",
             "tier": None, "model": None, "created_at": now},
            {"id": 2, "role": "assistant", "who": None, "content": "hello",
             "tier": "llm", "model": "claude-x", "created_at": now},
        ],
    )
    response = client.get("/chat")
    assert response.status_code == 200
    assert "chat__msg--thinking" not in response.text


def test_chat_page_linkifies_assistant_urls_but_not_user_text(client, monkeypatch):
    """NO markdown rendering (те саме рішення, що й brief.html): голий URL у
    відповіді асистента стає клікабельним через фільтр linkify, але текст
    людини — жодних посилань, навіть якщо вона сама вставила URL."""
    _login(client)
    now = datetime.now(timezone.utc)
    _fake_db(
        monkeypatch,
        rows=[
            {"id": 1, "role": "user", "who": WHO, "content": "see https://example.com",
             "tier": None, "model": None, "created_at": now},
            {"id": 2, "role": "assistant", "who": None, "content": "sure: https://example.com/x",
             "tier": "llm", "model": "claude-x", "created_at": now},
        ],
    )
    html = client.get("/chat").text
    assert '<a href="https://example.com/x" target="_blank" rel="noopener">' in html
    # Повідомлення людини лишається голим текстом — жодного <a> навколо нього.
    assert "see https://example.com</p>" in html


def test_chat_page_shows_stub_tier_chip(client, monkeypatch):
    _login(client)
    now = datetime.now(timezone.utc)
    _fake_db(
        monkeypatch,
        rows=[
            {"id": 1, "role": "assistant", "who": None, "content": "hi",
             "tier": "stub", "model": None, "created_at": now},
        ],
    )
    html = client.get("/chat").text
    assert "keyword tier" in html  # pg.chat.stub_chip, дефолт en


# ── GET /chat?ask=…: префил композера (розділ D2) ────────────────────


def test_chat_ask_param_prefills_the_composer_textarea(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch, rows=[])

    html = client.get("/chat", params={"ask": "which grants funded oracle work?"}).text
    assert (
        '<textarea id="chat-input" name="message" maxlength="4000" required'
        in html
    )
    assert "which grants funded oracle work?</textarea>" in html


def test_chat_ask_param_is_html_escaped_against_xss(client, monkeypatch):
    """Прямий тест на ін'єкцію: ask=<script>… має вийти escaped-текстом
    усередині textarea, а не виконуваним тегом — autoescape Jinja, без |safe."""
    _login(client)
    _fake_db(monkeypatch, rows=[])

    payload = "<script>alert(1)</script>"
    html = client.get("/chat", params={"ask": payload}).text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_chat_ask_param_is_truncated_to_4000_chars(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch, rows=[])

    html = client.get("/chat", params={"ask": "x" * 5000}).text
    assert "x" * 4001 not in html
    assert "x" * 4000 in html


def test_chat_page_without_ask_param_has_empty_textarea_body(client, monkeypatch):
    """Дефолт (без ?ask=) лишає textarea порожньою — жодного авто-заповнення,
    коли людина просто відкрила /chat."""
    _login(client)
    _fake_db(monkeypatch, rows=[])

    html = client.get("/chat").text
    assert "</textarea>" in html
    body_start = html.index('id="chat-input"')
    tail = html[body_start:]
    assert tail.split(">", 1)[1].split("</textarea>", 1)[0] == ""


# ── GET /chat: форумні чипи (розділ D4) ───────────────────────────────


def test_chat_page_renders_all_chip_and_enabled_forum_chips(client, monkeypatch):
    _login(client)
    _fake_db(
        monkeypatch,
        rows=[],
        extra_rows={1: [{"forum_slug": "optimism"}, {"forum_slug": "arbitrum"}]},
    )

    html = client.get("/chat").text
    assert 'data-forum=""' in html  # чип "All"
    assert 'data-forum="Optimism"' in html
    assert 'data-forum="Arbitrum"' in html
    # No-JS fallback href — ask= уже прив'язаний до конкретного форуму.
    assert "/chat?ask=Which%20dev%20tooling%20grants%20were%20discussed%20in%20Optimism" in html


def test_chat_page_forum_chips_empty_when_no_forums_enabled(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch, rows=[], extra_rows={1: []})

    html = client.get("/chat").text
    assert 'data-forum=""' in html  # "All" завжди присутній
    assert "chat__forumchips" in html


# ── POST /chat/send: валідація ──────────────────────────────────────


def test_chat_send_rejects_empty_message_redirect_branch(client):
    _login(client)
    response = client.post(
        "/chat/send",
        data={"message": "   ", "csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/chat?error=")


def test_chat_send_rejects_empty_message_json_branch(client):
    _login(client)
    response = client.post(
        "/chat/send",
        data={"message": "", "csrf": _csrf(client)},
        headers={**SAME_ORIGIN, "X-Requested-With": "fetch"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert body["error"]


def test_chat_send_rejects_message_over_4000_chars(client):
    _login(client)
    response = client.post(
        "/chat/send",
        data={"message": "x" * 4001, "csrf": _csrf(client)},
        headers={**SAME_ORIGIN, "X-Requested-With": "fetch"},
    )
    assert response.status_code == 400
    assert "4000" in response.json()["error"]


# ── POST /chat/send: тумблер веб-пошуку (розділ C) ───────────────────


def test_chat_send_passes_web_true_when_checkbox_checked(client, monkeypatch):
    """Checkbox `name="web" value="1"` — коли позначений, payload до kbmcp
    несе `"web": True` (контракт: опціональне булеве поле)."""
    _login(client)
    seen = {}

    def fake_backend(payload):
        seen.update(payload)
        return {"ok": True, "reply_md": "ok", "tier": "llm", "model": None}

    monkeypatch.setattr(admin_app, "_chat_backend", fake_backend)
    response = client.post(
        "/chat/send",
        data={"message": "hello", "web": "1", "csrf": _csrf(client)},
        headers={**SAME_ORIGIN, "X-Requested-With": "fetch"},
    )
    assert response.status_code == 200
    assert seen["web"] is True


def test_chat_send_omits_web_key_when_checkbox_absent(client, monkeypatch):
    """Непозначений чекбокс браузер узагалі не надсилає (стандартна
    поведінка форм) — payload до kbmcp не повинен мати ключ "web" зовсім."""
    _login(client)
    seen = {}

    def fake_backend(payload):
        seen.update(payload)
        return {"ok": True, "reply_md": "ok", "tier": "llm", "model": None}

    monkeypatch.setattr(admin_app, "_chat_backend", fake_backend)
    response = client.post(
        "/chat/send",
        data={"message": "hello", "csrf": _csrf(client)},  # без "web"
        headers={**SAME_ORIGIN, "X-Requested-With": "fetch"},
    )
    assert response.status_code == 200
    assert "web" not in seen


def test_chat_send_omits_web_key_when_checkbox_value_is_empty_string(client, monkeypatch):
    """Той самий Form("") дефолт, яким FastAPI ловить і відсутнє поле, і
    порожнє значення — обидва мають давати однакову поведінку."""
    _login(client)
    seen = {}

    def fake_backend(payload):
        seen.update(payload)
        return {"ok": True, "reply_md": "ok", "tier": "llm", "model": None}

    monkeypatch.setattr(admin_app, "_chat_backend", fake_backend)
    response = client.post(
        "/chat/send",
        data={"message": "hello", "web": "", "csrf": _csrf(client)},
        headers={**SAME_ORIGIN, "X-Requested-With": "fetch"},
    )
    assert response.status_code == 200
    assert "web" not in seen


def test_chat_composer_renders_web_search_checkbox(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch, rows=[])

    html = client.get("/chat").text
    assert '<input type="checkbox" id="chat-web" name="web" value="1">' in html
    assert "Web search" in html


# ── POST /chat/send: гілкування відповіді за X-Requested-With ───────


def test_chat_send_json_branch_returns_reply_without_writing_to_db(client, monkeypatch):
    """Admin сам НЕ пише в kb.chat_messages (пише kbmcp — розділ 4.9), тож
    db() тут узагалі торкатися не мав би: перевіряємо непрямо, підмінивши її
    на об'єкт, що кидає AssertionError при будь-якому виклику."""
    _login(client)

    class _NoDb:
        def __call__(self):
            raise AssertionError("send_chat_message не мав звертатися до БД")

    monkeypatch.setattr(admin_app, "db", _NoDb())
    monkeypatch.setattr(
        admin_app,
        "_chat_backend",
        lambda payload: {"ok": True, "reply_md": "hi there", "tier": "llm", "model": "claude-x"},
    )
    response = client.post(
        "/chat/send",
        data={"message": "hello", "csrf": _csrf(client)},
        headers={**SAME_ORIGIN, "X-Requested-With": "fetch"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {"ok": True, "reply": "hi there", "tier": "llm", "model": "claude-x"}


def test_chat_send_session_key_is_raw_without_namespace(client, monkeypatch):
    """Контракт kbmcp /chat: session_key БЕЗ ':' — неймспейс "web:" додає
    kbmcp при записі. Регресія знайдена на живому smoke 2026-08-06: admin
    надсилав уже неймспейснутий ключ, kbmcp відповідав 400, а замокані тести
    цього не бачили — тому фейковий бекенд тут повторює валідацію реального.
    """
    _login(client)
    seen = {}

    def fake_backend(payload):
        seen.update(payload)
        assert ":" not in payload["session_key"], (
            "session_key має бути сирим — kbmcp неймспейсить сам"
        )
        return {"ok": True, "reply_md": "ok", "tier": "stub", "model": None}

    monkeypatch.setattr(admin_app, "_chat_backend", fake_backend)
    response = client.post(
        "/chat/send",
        data={"message": "hello", "csrf": _csrf(client)},
        headers={**SAME_ORIGIN, "X-Requested-With": "fetch"},
    )
    assert response.status_code == 200
    assert seen["channel"] == "web"
    assert seen["session_key"]  # непорожній сирий ключ (sid або legacy-<who>)


def test_chat_send_redirect_branch_without_fetch_header(client, monkeypatch):
    """Без X-Requested-With: fetch (звичайний браузер) — 303 на /chat, PRG:
    сторінка мусить повністю працювати без JS."""
    _login(client)
    monkeypatch.setattr(
        admin_app, "_chat_backend",
        lambda payload: {"ok": True, "reply_md": "hi", "tier": "llm", "model": None},
    )
    response = client.post(
        "/chat/send",
        data={"message": "hello", "csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/chat"


def test_chat_send_passes_who_and_raw_sid_as_session_key(client, monkeypatch):
    """session_key у бекенд іде СИРИМ (sid без "web:") — неймспейс додає
    kbmcp при записі; ':' у ключі контракт /chat відхиляє 400-кою."""
    _login(client)
    sid = auth.session_sid(
        type("R", (), {"cookies": {auth.COOKIE_BASE: client.cookies[auth.COOKIE_BASE]}})()
    )
    seen = {}

    def fake_backend(payload):
        seen.update(payload)
        return {"ok": True, "reply_md": "ok", "tier": "llm", "model": None}

    monkeypatch.setattr(admin_app, "_chat_backend", fake_backend)
    client.post(
        "/chat/send",
        data={"message": "hello", "csrf": _csrf(client)},
        headers={**SAME_ORIGIN, "X-Requested-With": "fetch"},
    )
    assert seen["channel"] == "web"
    assert seen["session_key"] == sid
    assert seen["who"] == WHO
    assert seen["message"] == "hello"


# ── POST /chat/send: помилки бекенда НЕ ковтаються мовчки ───────────


def test_chat_send_surfaces_kbmcp_ok_false_error(client, monkeypatch):
    monkeypatch.setattr(
        admin_app, "_chat_backend",
        lambda payload: {"ok": False, "error": "rate limited, try later"},
    )
    _login(client)
    response = client.post(
        "/chat/send",
        data={"message": "hello", "csrf": _csrf(client)},
        headers={**SAME_ORIGIN, "X-Requested-With": "fetch"},
    )
    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "rate limited, try later"}

    # Той самий шлях без fetch-заголовка — помилка потрапляє в query, а не
    # губиться в мовчазному редіректі (на відміну від generate_brief вище,
    # де для помилки немає JS-гілки, тож фрагмент — прийнятний компроміс).
    response = client.post(
        "/chat/send",
        data={"message": "hello", "csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "rate+limited" in response.headers["location"] or "rate%20limited" in response.headers["location"]


def test_chat_send_maps_httpx_error_to_visible_error(client, monkeypatch):
    """Мережа впала (kbmcp недоступний) — httpx.HTTPError, не 500 і не
    порожня відповідь."""
    _login(client)

    def boom(payload):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(admin_app, "_chat_backend", boom)
    response = client.post(
        "/chat/send",
        data={"message": "hello", "csrf": _csrf(client)},
        headers={**SAME_ORIGIN, "X-Requested-With": "fetch"},
    )
    assert response.status_code == 502
    body = response.json()
    assert body["ok"] is False
    assert body["error"]


# ── POST /chat/new: ротація ──────────────────────────────────────────


def test_chat_new_redirects_and_rotates_cookie(client):
    _login(client)
    before = client.cookies[auth.COOKIE_BASE]
    response = client.post(
        "/chat/new",
        data={"csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/chat"
    after = response.cookies.get(auth.COOKIE_BASE)
    assert after and after != before


# ── POST /chat/save-report: «Зберегти чат як звіт» (розділ D) ─────────


def test_save_chat_report_happy_path_redirects_to_new_brief(client, monkeypatch):
    monkeypatch.setattr(
        admin_app, "_chat_report_backend",
        lambda payload: {"ok": True, "brief_id": 77, "title": "Chat report"},
    )
    _login(client)
    response = client.post(
        "/chat/save-report",
        data={"csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/briefs/77"


def test_save_chat_report_uses_raw_session_key_without_namespace(client, monkeypatch):
    """Той самий контракт, що й /chat/send: session_key СИРИЙ (без "web:") —
    kbmcp сам неймспейсить його при читанні kb.chat_messages."""
    _login(client)
    sid = _sid(client)
    seen = {}

    def fake_backend(payload):
        seen.update(payload)
        return {"ok": True, "brief_id": 1, "title": "x"}

    monkeypatch.setattr(admin_app, "_chat_report_backend", fake_backend)
    client.post(
        "/chat/save-report",
        data={"csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert seen["channel"] == "web"
    assert seen["session_key"] == sid
    assert ":" not in seen["session_key"]


def test_save_chat_report_surfaces_kbmcp_ok_false_error(client, monkeypatch):
    """kbmcp-помилка (наприклад, «нічого синтезувати» на порожню розмову) —
    той самий шлях помилки, що й /chat/send: назад на /chat з ?error=…"""
    monkeypatch.setattr(
        admin_app, "_chat_report_backend",
        lambda payload: {"ok": False, "error": "nothing to summarize"},
    )
    _login(client)
    response = client.post(
        "/chat/save-report",
        data={"csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/chat?error=")
    assert "nothing" in response.headers["location"].replace("+", " ")


def test_save_chat_report_maps_httpx_error_to_chat_error_state(client, monkeypatch):
    """Мережа впала (kbmcp недоступний) — не 500, а той самий редірект з
    поясненням, що й для kbmcp ok:false вище."""
    import httpx

    def boom(payload):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(admin_app, "_chat_report_backend", boom)
    _login(client)
    response = client.post(
        "/chat/save-report",
        data={"csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/chat?error=")


def test_chat_page_hides_save_report_button_when_conversation_is_empty(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch, rows=[])
    html = client.get("/chat").text
    assert "Save chat as report" not in html
    assert 'action="/chat/save-report"' not in html


def test_chat_page_shows_save_report_button_when_conversation_has_messages(client, monkeypatch):
    _login(client)
    now = datetime.now(timezone.utc)
    _fake_db(
        monkeypatch,
        rows=[
            {"id": 1, "role": "user", "who": WHO, "content": "hi",
             "tier": None, "model": None, "created_at": now},
        ],
    )
    html = client.get("/chat").text
    assert "Save chat as report" in html
    assert 'action="/chat/save-report"' in html


# ── POST /chat/save-brief: «Save as brief» (розділ B) ─────────────────


def _sid(client) -> str:
    return auth.session_sid(
        type("R", (), {"cookies": {auth.COOKIE_BASE: client.cookies[auth.COOKIE_BASE]}})()
    )


def test_save_chat_message_as_brief_happy_path_redirects_to_new_brief(client, monkeypatch):
    """Три звернення до БД по черзі: (0) сам рядок, (1) попереднє питання
    людини — заголовок бріфа, (2) INSERT ... RETURNING id. `_Conn.extra_rows`
    індексує рівно за цим порядком викликів."""
    _login(client)
    session_key = f"web:{_sid(client)}"
    sink: list = []
    _fake_db(
        monkeypatch,
        rows=[{
            "session_key": session_key, "role": "assistant", "tier": "llm",
            "model": "claude-x", "content": "Here is the brief text.",
        }],
        sink=sink,
        extra_rows={
            1: [{"content": "What grants exist for oracle work?"}],
            2: [{"id": 42}],
        },
    )
    response = client.post(
        "/chat/save-brief",
        data={"message_id": "7", "csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/briefs/42"

    insert_sql, insert_params = sink[2]
    assert "INSERT INTO kb.briefs" in insert_sql
    assert insert_params == (
        "What grants exist for oracle work?",
        "Here is the brief text.",
        "llm",
        "claude-x",
    )


def test_save_chat_message_as_brief_falls_back_to_chat_answer_title(client, monkeypatch):
    """Найперший рядок сесії — попереднього user-повідомлення просто нема."""
    _login(client)
    session_key = f"web:{_sid(client)}"
    sink: list = []
    _fake_db(
        monkeypatch,
        rows=[{
            "session_key": session_key, "role": "assistant", "tier": "llm",
            "model": None, "content": "reply with no preceding question",
        }],
        sink=sink,
        extra_rows={1: [], 2: [{"id": 5}]},
    )
    response = client.post(
        "/chat/save-brief",
        data={"message_id": "1", "csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    _, insert_params = sink[2]
    assert insert_params[0] == "Chat answer"


def test_save_chat_message_as_brief_rejects_another_sessions_message(client, monkeypatch):
    """Guard 2 (розділ B docstring): session_key рядка ≠ сесія браузера —
    ID-перебір не має давати доступ до чужих (телеграм чи іншого члена
    команди) відповідей."""
    _login(client)
    _fake_db(
        monkeypatch,
        rows=[{
            "session_key": "web:someone-elses-sid", "role": "assistant", "tier": "llm",
            "model": "claude-x", "content": "not yours",
        }],
    )
    response = client.post(
        "/chat/save-brief",
        data={"message_id": "7", "csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 404


def test_save_chat_message_as_brief_rejects_non_assistant_role(client, monkeypatch):
    """Guard 1: role='user' — форма шле лише message_id, тож підміна в
    DevTools не має дати зберегти власне питання людини як «бріф»."""
    _login(client)
    session_key = f"web:{_sid(client)}"
    _fake_db(
        monkeypatch,
        rows=[{
            "session_key": session_key, "role": "user", "tier": None,
            "model": None, "content": "my own question",
        }],
    )
    response = client.post(
        "/chat/save-brief",
        data={"message_id": "3", "csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 404


def test_save_chat_message_as_brief_rejects_stub_tier(client, monkeypatch):
    """Guard 1, друга половина: tier='stub' — кнопка в chat.html не
    рендериться під такими бульбашками, хендлер не повинен довіряти клієнту."""
    _login(client)
    session_key = f"web:{_sid(client)}"
    _fake_db(
        monkeypatch,
        rows=[{
            "session_key": session_key, "role": "assistant", "tier": "stub",
            "model": None, "content": "keyword-tier reply",
        }],
    )
    response = client.post(
        "/chat/save-brief",
        data={"message_id": "9", "csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 404


def test_save_chat_message_as_brief_404_when_message_missing(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch, rows=[])
    response = client.post(
        "/chat/save-brief",
        data={"message_id": "999", "csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 404
