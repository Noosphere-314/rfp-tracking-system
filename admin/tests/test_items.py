"""Тести /items — фільтри (розділ 4.3, розширення) і POST .../outcome.

Без БД і без мережі: `admin.app.db` тут завжди підмінений monkeypatch'ем на
об'єкт, що лише записує SQL+параметри в `sink` (той самий прийом, що й у
test_chat.py). Сесія й CSRF — ловляться загальними тестами в test_auth.py
(`test_every_get_route_requires_a_session` ітерує `app.routes`, тож новий GET
/items автоматично під наглядом; так само POST .../outcome — під
`test_every_post_route_is_csrf_covered`, бо він на роутері `mutations`).

Імпорт test_auth ПЕРШИМ — env (DASHBOARD_PASSWORD, SESSION_SECRET, …)
виставляється до першого імпорту admin.app (той самий трюк, що й у
test_chat.py / test_md_lite.py).
"""

from __future__ import annotations

from admin.tests.test_auth import SAME_ORIGIN, WHO, _login, client  # noqa: E402,F401

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
    """Записує (sql, params) КОЖНОГО execute() у sink — тести читають перший
    елемент (основний запит /items) і перевіряють лише текст WHERE, не
    рушійну БД.

    Перший execute() бачить `rows` (сумісність з тестами, писаними до появи
    другого запиту /items — ecosystem_options). Кожен НАСТУПНИЙ виклик за
    замовчуванням отримує порожній список, а не ті самі `rows`: інакше рядки
    знахідок (item_uid/title/…) підставлялися б замість рядків
    `SELECT DISTINCT ecosystem FROM sources` (ecosystem) і падали б KeyError.
    `extra_rows` дозволяє тесту задати результат саме для другого (і далі)
    виклику за індексом, коли це потрібно.
    """

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
        return _Cursor(result_rows)

    def commit(self):
        pass


def _fake_db(monkeypatch, rows=None, sink=None, extra_rows=None):
    conn = _Conn(rows, sink, extra_rows)
    monkeypatch.setattr(admin_app, "db", lambda: conn)
    return conn


def _csrf(client) -> str:
    return auth.csrf_for(client.cookies[auth.COOKIE_BASE])


# ── _ilike_term: чиста функція, екранування LIKE-метасимволів ────────────


def test_ilike_term_escapes_percent_and_underscore_and_backslash():
    assert admin_app._ilike_term("50% off") == "%50\\% off%"
    assert admin_app._ilike_term("a_b") == "%a\\_b%"
    assert admin_app._ilike_term("a\\b") == "%a\\\\b%"
    assert admin_app._ilike_term("plain") == "%plain%"


# ── GET /items: побудова WHERE — дефолт незмінний ────────────────────────


def test_items_default_view_has_no_where_clause(client, monkeypatch):
    """Без жодного параметра запит лишається ТОЧНО таким, як був до
    розширення (F5-інваріант): нове посилання з бекфіловими фільтрами не
    повинно міняти сенс старих закладок /items."""
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)

    response = client.get("/items")
    assert response.status_code == 200
    sql, params = sink[0]
    assert "WHERE" not in sql
    assert params == (0,)  # лише OFFSET


def test_items_all_filters_are_combinable(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)

    response = client.get(
        "/items",
        params={
            "status": "pending",
            "q": "50% grant_x",
            "ecosystem": "Optimism",
            "lane": "rfp",
            "min_confidence": "0.7",
            "period": "30d",
            "outcome": "won",
        },
    )
    assert response.status_code == 200
    sql, params = sink[0]
    assert "i.status = %s" in sql
    assert "i.title ILIKE %s" in sql
    assert "s.ecosystem = %s" in sql
    assert "s.lane = %s" in sql
    assert "i.confidence >= %s" in sql
    assert "i.first_seen > now() - interval '30 days'" in sql
    assert "i.outcome = 'won'" in sql
    assert params == (
        "pending", "%50\\% grant\\_x%", "Optimism", "rfp", 0.7, 0,
    )


def test_items_invalid_min_confidence_and_period_are_ignored(client, monkeypatch):
    """Значення поза фіксованим набором опцій (select у items.html) не
    падають 400-кою — старе посилання з відкликаною опцією просто не
    фільтрує, а не ламається."""
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)

    response = client.get("/items", params={"min_confidence": "0.99", "period": "1y"})
    assert response.status_code == 200
    sql, _ = sink[0]
    assert "WHERE" not in sql


def test_items_outcome_open_means_delivered_without_a_verdict(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)

    response = client.get("/items", params={"outcome": "open"})
    assert response.status_code == 200
    sql, _ = sink[0]
    assert "i.delivered_at IS NOT NULL AND i.outcome IS NULL" in sql


def test_items_page_offset_multiplies_by_page_size(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)

    client.get("/items", params={"page": "2"})
    _, params = sink[0]
    assert params[-1] == 100


# ── «Ask AI» (розділ D3): посилання на /chat?ask=… з готовим питанням ────


def _item_row(**overrides) -> dict:
    from datetime import datetime, timezone

    row = {
        "item_uid": "uid-abc123",
        "title": "Fund a cross-chain oracle relayer",
        "canonical_project": None,
        "status": "pending",
        "url": "https://example.test/rfp/1",
        "category": None,
        "confidence": 0.72,
        "source_name": "Some Forum",
        "source_ecosystem": "Optimism",
        "attempts": 1,
        "first_seen": datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc),
        "delivered_at": None,
        "outcome": None,
    }
    row.update(overrides)
    return row


def test_ask_ai_question_builds_english_question_with_ecosystem_and_title():
    question = admin_app._ask_ai_question("Fund an oracle relayer", "Optimism", "uid-1")
    assert question == (
        'What has Optimism funded similar to "Fund an oracle relayer"? '
        "Who decides and what are typical amounts?"
    )


def test_ask_ai_question_falls_back_to_item_uid_and_generic_ecosystem():
    # Рівно 16 символів — item_uid[:16] дає той самий рядок цілком, без
    # залежності тесту від точного зрізу.
    uid = "a1b2c3d4e5f6g7h8"
    assert len(uid) == 16
    question = admin_app._ask_ai_question(None, None, uid)
    assert f'"{uid}"' in question
    assert "this ecosystem" in question


def test_ask_ai_question_truncates_long_titles():
    long_title = "x" * 250
    question = admin_app._ask_ai_question(long_title, "Optimism", "uid-1")
    # Обрізаний заголовок коротший за оригінал і закінчується «…».
    assert "x" * 250 not in question
    assert "…" in question


def test_items_page_renders_ask_ai_link_urlencoded(client, monkeypatch):
    """Посилання «Ask AI» на рядку знахідки веде на /chat?ask=<урленкодоване
    англійське питання>, зібране в app.py (не в шаблоні)."""
    _login(client)
    _fake_db(monkeypatch, rows=[_item_row()])

    html = client.get("/items").text

    expected_question = admin_app._ask_ai_question(
        "Fund a cross-chain oracle relayer", "Optimism", "uid-abc123"
    )
    from urllib.parse import quote

    expected_href = "/chat?ask=" + quote(expected_question, safe="")
    assert expected_href in html
    assert "%20" in expected_href  # урленкодоване: пробіли не лишились сирими
    # Сирий, неенкодований пробіл між словами питання у href не з'являється.
    assert 'ask=What has Optimism' not in html
    assert "Ask AI" in html


# ── POST /items/{uid}/outcome ─────────────────────────────────────────────


def test_set_outcome_won_writes_actor_and_redirects_with_qs(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, sink=sink)

    response = client.post(
        "/items/abc123/outcome",
        data={"outcome": "won", "qs": "status=pending&page=1", "csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/items?status=pending&page=1"

    sql, params = sink[0]
    assert "outcome = %s" in sql and "outcome_by = %s" in sql and "outcome_at = now()" in sql
    assert params == ("won", f"admin-ui:{WHO}", "abc123")


def test_set_outcome_lost(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, sink=sink)

    response = client.post(
        "/items/abc123/outcome",
        data={"outcome": "lost", "csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/items"  # порожній qs -> без "?"
    sql, params = sink[0]
    assert params == ("lost", f"admin-ui:{WHO}", "abc123")


def test_set_outcome_clear_nulls_all_three_columns(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, sink=sink)

    response = client.post(
        "/items/abc123/outcome",
        data={"outcome": "clear", "csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    sql, params = sink[0]
    assert "outcome = NULL" in sql and "outcome_by = NULL" in sql and "outcome_at = NULL" in sql
    assert params == ("abc123",)


def test_set_outcome_rejects_invalid_value(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(admin_app, "db", lambda: (_ for _ in ()).throw(
        AssertionError("не мав дійти до БД")
    ))
    response = client.post(
        "/items/abc123/outcome",
        data={"outcome": "maybe", "csrf": _csrf(client)},
        headers=SAME_ORIGIN,
    )
    assert response.status_code == 400


# CSRF-покриття /items/{item_uid}/outcome НЕ дублюється окремим тестом тут:
# `test_every_post_route_is_csrf_covered` у test_auth.py вже ітерує
# `app.routes` і ловить БУДЬ-ЯКИЙ новий POST без `Depends(csrf_guard)`, у тому
# числі цей маршрут (він на роутері `mutations`, як і всі інші). Окрема
# перевірка ідентичності функції тут була б крихкою: test_auth.py має тест,
# що робить `importlib.reload(auth)` (перевірка порожнього DASHBOARD_USERS),
# і після нього `auth.csrf_guard` — вже ІНШИЙ об'єкт функції, ніж той, що
# captured у `mutations` при імпорті admin.app; порядок файлів pytest
# (test_auth.py < test_items.py) означає, що такий тест тут ловив би
# несправжній негатив, залежний від порядку колекції, а не від реальної
# відсутності захисту.
