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
    # Єдиний WHERE — усередині LATERAL-підзапиту brief_id (задача «Open
    # brief» 2026-08-11); ЗОВНІШНІЙ запит без фільтрів, як і був.
    assert sql.count("WHERE") == 1 and "WHERE b.item_uid" in sql
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
    # Див. коментар у тесті дефолтного вигляду: 1 WHERE = лише LATERAL.
    assert sql.count("WHERE") == 1 and "WHERE b.item_uid" in sql


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


# ── /items?view=… — серверні пресети (розділ A/B3) ────────────────────────


def test_items_view_review24_ignores_every_other_filter(client, monkeypatch):
    """`view=review24` — самодостатній зріз (посилання з ранкового
    Telegram-дайджесту): усі інші query-параметри, навіть якщо присутні,
    НЕ впливають на WHERE/params — лише offset лишається."""
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)

    response = client.get(
        "/items",
        params={
            "view": "review24",
            "status": "pending", "q": "ignored", "ecosystem": "Optimism",
            "lane": "rfp", "min_confidence": "0.7", "period": "7d",
            "outcome": "won",
        },
    )
    assert response.status_code == 200
    sql, params = sink[0]
    assert "i.category = 'FUNDING'" in sql
    assert "i.delivered_at IS NULL" in sql
    assert "i.first_seen > now() - interval '24 hours'" in sql
    assert "ORDER BY i.confidence DESC NULLS LAST" in sql
    assert "i.status = %s" not in sql
    assert "i.title ILIKE %s" not in sql
    assert params == (0,)  # лише OFFSET — жоден фільтр не додав плейсхолдер


def test_items_view_review24_shows_banner_with_clear_view_link(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch, rows=[])

    html = client.get("/items", params={"view": "review24"}).text
    assert "Morning review queue" in html
    assert "this page IS the daily report" in html
    assert 'href="/items">clear view<' in html


def test_items_default_view_has_no_review24_banner(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch, rows=[])

    html = client.get("/items").text
    assert "Morning review queue" not in html


def test_items_view_leads24_preset_where_and_ignores_filters(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)

    response = client.get(
        "/items", params={"view": "leads24", "status": "pending"}
    )
    assert response.status_code == 200
    sql, params = sink[0]
    assert "i.delivered_at > now() - interval '24 hours'" in sql
    assert "i.status = %s" not in sql
    assert params == (0,)


def test_items_view_leads24_has_no_review24_banner(client, monkeypatch):
    """Банер розділу A прив'язаний ЛИШЕ до view=review24 — leads24 не
    отримує чужого пояснення."""
    _login(client)
    _fake_db(monkeypatch, rows=[])

    html = client.get("/items", params={"view": "leads24"}).text
    assert "Morning review queue" not in html


def test_items_unknown_view_value_falls_back_to_normal_filters(client, monkeypatch):
    """Невідоме значення `view` (стара/відкликана посилання) не ламає
    сторінку — той самий інваріант терпимості, що й у
    min_confidence/period вище: фільтри застосовуються як завжди."""
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)

    response = client.get("/items", params={"view": "bogus", "status": "pending"})
    assert response.status_code == 200
    sql, params = sink[0]
    assert "i.status = %s" in sql
    assert "WHERE" in sql
    assert params == ("pending", 0)


def test_items_view_review24_hides_the_ordinary_filters_active_banner(client, monkeypatch):
    """Пресет — не «ще один фільтр», тож звичайний банер «Filters active —
    Reset» (для комбінованих фільтрів вище) тут не рендериться; своє
    пояснення дає окремий банер розділу A, перевірений вище."""
    _login(client)
    _fake_db(monkeypatch, rows=[])

    html = client.get("/items", params={"view": "review24"}).text
    assert "Filters active" not in html


def test_items_view_review24_pager_link_keeps_the_view_param(client, monkeypatch):
    """Лінк «older» (рендериться лише коли сторінка повна — 50 рядків)
    зберігає `view=review24` — інакше друга сторінка пресету губила б сам
    пресет."""
    _login(client)
    rows = [_item_row(item_uid=f"uid-{i:03d}") for i in range(50)]
    _fake_db(monkeypatch, rows=rows)

    html = client.get("/items", params={"view": "review24"}).text
    # Jinja автоескейпить `&` у href як `&amp;` — той самий інваріант, що й
    # у решті сторінки (розділ 4.3: qs_no_page завжди йде крізь атрибут href).
    assert "/items?view=review24&amp;page=1" in html


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
        # Задача 2 аудиту 2026-08-12 — NULL = ще не оцінено (seen_items.useful,
        # міграція 014). Явний дефолт тут, а не Jinja Undefined: шаблон
        # перевіряє `i.useful is none`, і Undefined (ключ відсутній узагалі)
        # НЕ проходить цю перевірку так само, як None — тест тихо показував
        # би невірну гілку (badge замість кнопок 👍/👎).
        "useful": None,
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


# ── Розділ B1: рядок-лід (delivered_at IS NOT NULL) ───────────────────────


def test_items_page_delivered_row_gets_lead_class_and_chip(client, monkeypatch):
    from datetime import datetime, timezone

    _login(client)
    row = _item_row(delivered_at=datetime(2026, 1, 6, 9, 0, tzinfo=timezone.utc))
    _fake_db(monkeypatch, rows=[row])

    html = client.get("/items").text
    assert '<tr class="item-row--lead">' in html
    assert "LEAD" in html


def test_items_page_undelivered_row_has_no_lead_class_or_chip(client, monkeypatch):
    _login(client)
    row = _item_row(delivered_at=None)
    _fake_db(monkeypatch, rows=[row])

    html = client.get("/items").text
    assert "item-row--lead" not in html
    assert "LEAD" not in html


# ── Розділ B2: NAV-бейдж «нових лідів за 24 год» (context processor) ──────


def test_items_page_nav_badge_shows_unread_count_next_to_findings(client, monkeypatch):
    """`_leads_badge_context` (app.py) рахується один раз на рендер і
    доступний глобально — /items тут лише зручна сторінка для перевірки,
    бейдж живе в base.html і рендериться на кожній. З 2026-08-11 бейдж —
    НЕПРОЧИТАНІ (unread_count, .b-info), а не ліди за 24 год."""
    _login(client)
    # Третій виклик execute() у цьому рендері (0: items, 1: ecosystem
    # options, 2: контекст-процесор бейджа NAV) — той самий порядок, що
    # й guard-коментар у _leads_badge_context описує.
    _fake_db(monkeypatch, rows=[],
             extra_rows={2: [{"leads_24h": 3, "unread_count": 7}]})

    html = client.get("/items").text
    assert 'class="badge b-info"' in html
    assert ">7<" in html


def test_items_page_nav_badge_hidden_when_no_recent_leads(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch, rows=[])

    html = client.get("/items").text
    assert 'class="badge b-lead"' not in html


def test_items_page_nav_badge_absent_when_leads_query_fails(client, monkeypatch):
    """Помилка БД у контекст-процесорі не має валити сторінку — бейдж лише
    підказка (той самий компроміс, що й /healthz)."""
    _login(client)
    calls = {"n": 0}

    def flaky_db():
        # items_page робить РІВНО один виклик db() (один `with`-блок на
        # обидва свої execute()); другий виклик db() — уже
        # _leads_badge_context (окремий `with` нижче за течією рендеру),
        # і саме він тут падає.
        calls["n"] += 1
        if calls["n"] == 1:
            return _Conn(rows=[])
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(admin_app, "db", flaky_db)
    response = client.get("/items")
    assert response.status_code == 200
    assert 'class="badge b-lead"' not in response.text


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


# ── Задача 1 аудиту 2026-08-12: дієта колонок — Attempts/Delivered у title= ─


def test_items_page_status_badge_carries_attempts_in_title(client, monkeypatch):
    _login(client)
    row = _item_row(status="pending", attempts=3)
    _fake_db(monkeypatch, rows=[row])

    html = client.get("/items").text
    assert 'title="Attempts: 3"' in html
    # Колонки «Attempts» окремо більше немає.
    assert "<th>Attempts</th>" not in html


def test_items_page_lead_chip_carries_delivered_date_in_title(client, monkeypatch):
    from datetime import datetime, timezone

    _login(client)
    row = _item_row(delivered_at=datetime(2026, 1, 6, 9, 30, tzinfo=timezone.utc))
    _fake_db(monkeypatch, rows=[row])

    html = client.get("/items").text
    assert "2026-01-06 09:30" in html
    assert "<th>Delivered</th>" not in html


def test_items_page_verdict_column_merges_category_and_confidence(client, monkeypatch):
    _login(client)
    row = _item_row(category="FUNDING", confidence=0.83)
    _fake_db(monkeypatch, rows=[row])

    html = client.get("/items").text
    assert "<th>Verdict</th>" in html
    assert "<th>Category</th>" not in html
    assert "<th>Confidence</th>" not in html
    assert "b-funding" in html and "0.83" in html


def test_items_page_source_cell_is_truncated_with_full_name_in_title(client, monkeypatch):
    _login(client)
    row = _item_row(source_name="A Very Long Forum Name That Should Truncate")
    _fake_db(monkeypatch, rows=[row])

    html = client.get("/items").text
    assert 'cell--truncate-sm' in html
    assert 'title="A Very Long Forum Name That Should Truncate"' in html


def test_items_page_column_order_is_status_title_verdict_source_seen_outcome_actions(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch, rows=[])

    html = client.get("/items").text
    order = ["Status", "Title", "Verdict", "Source", "First seen", "Outcome"]
    positions = [html.index(f"<th>{label}</th>") for label in order]
    assert positions == sorted(positions)


# ── Задача 2 аудиту 2026-08-12: 👍/👎 useful на не-лід рядках ─────────────


def test_items_page_shows_useful_buttons_for_pending_row_without_a_lead(client, monkeypatch):
    _login(client)
    row = _item_row(status="pending", delivered_at=None, useful=None)
    _fake_db(monkeypatch, rows=[row])

    html = client.get("/items").text
    assert 'action="/items/uid-abc123/useful"' in html
    assert 'value="yes"' in html and 'value="no"' in html
    # Текстові кнопки в семантичних відтінках замість емодзі (2026-08-12).
    assert "btn--ok" in html and 'value="yes"' in html and 'value="no"' in html


def test_items_page_shows_rated_badge_and_clear_when_useful_is_set(client, monkeypatch):
    _login(client)
    row = _item_row(status="done", delivered_at=None, useful=True)
    _fake_db(monkeypatch, rows=[row])

    html = client.get("/items").text
    assert "useful" in html
    # Рейтинг УЖЕ виставлено — форм із value="yes"/"no" (кнопки 👍/👎) для
    # цього рядка більше немає, лишається лише badge + «clear». Перевіряємо
    # саме форми, а не самі емодзі: ті легітимно лишаються в title=
    # фільтра «Usefulness» вище на сторінці (f.useful.hint).
    assert 'value="yes"' not in html and 'value="no"' not in html
    assert 'action="/items/uid-abc123/useful"' in html
    assert 'value="clear"' in html


def test_items_page_shows_noise_badge_when_useful_is_false(client, monkeypatch):
    _login(client)
    row = _item_row(status="done", delivered_at=None, useful=False)
    _fake_db(monkeypatch, rows=[row])

    html = client.get("/items").text
    assert "noise" in html


def test_items_page_filtered_row_shows_dash_not_useful_buttons(client, monkeypatch):
    _login(client)
    row = _item_row(status="filtered", delivered_at=None, useful=None)
    _fake_db(monkeypatch, rows=[row])

    html = client.get("/items").text
    assert "/useful" not in html


def test_items_page_seeded_row_shows_dash_not_useful_buttons(client, monkeypatch):
    _login(client)
    row = _item_row(status="seeded", delivered_at=None, useful=None)
    _fake_db(monkeypatch, rows=[row])

    html = client.get("/items").text
    assert "/useful" not in html


def test_items_page_lead_row_shows_outcome_buttons_not_useful_buttons(client, monkeypatch):
    """Лід (delivered_at) завжди показує Won/Lost, навіть коли useful IS NULL
    — дві оцінки взаємовиключні на одному рядку (розділ задачі 2)."""
    from datetime import datetime, timezone

    _login(client)
    row = _item_row(
        delivered_at=datetime(2026, 1, 6, 9, 0, tzinfo=timezone.utc), useful=None
    )
    _fake_db(monkeypatch, rows=[row])

    html = client.get("/items").text
    assert "/useful" not in html
    assert "Won" in html and "Lost" in html


def test_items_useful_filter_maps_to_where_clause(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)

    response = client.get("/items", params={"useful": "useful"})
    assert response.status_code == 200
    sql, _ = sink[0]
    assert "i.useful IS TRUE" in sql


def test_items_noise_filter_maps_to_where_clause(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)

    response = client.get("/items", params={"useful": "noise"})
    assert response.status_code == 200
    sql, _ = sink[0]
    assert "i.useful IS FALSE" in sql


def test_items_unrated_filter_scopes_to_done_and_pending_status(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)

    response = client.get("/items", params={"useful": "unrated"})
    assert response.status_code == 200
    sql, _ = sink[0]
    assert "i.useful IS NULL AND i.status IN ('done', 'pending')" in sql


def test_items_unknown_useful_value_is_ignored(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, rows=[], sink=sink)

    response = client.get("/items", params={"useful": "bogus"})
    assert response.status_code == 200
    sql, params = sink[0]
    assert "i.useful" not in sql
    assert params == (0,)


# ── POST /items/{uid}/useful ───────────────────────────────────────────────


def test_set_useful_yes_writes_true_and_redirects_with_qs(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, sink=sink)

    response = client.post(
        "/items/abc123/useful",
        data={"value": "yes", "qs": "status=pending&page=1", "csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/items?status=pending&page=1"
    sql, params = sink[0]
    assert "useful = %s" in sql
    assert params == (True, "abc123")


def test_set_useful_no_writes_false(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, sink=sink)

    response = client.post(
        "/items/abc123/useful",
        data={"value": "no", "csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/items"
    sql, params = sink[0]
    assert params == (False, "abc123")


def test_set_useful_clear_nulls_the_column(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, sink=sink)

    response = client.post(
        "/items/abc123/useful",
        data={"value": "clear", "csrf": _csrf(client)},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    sql, params = sink[0]
    assert "useful = NULL" in sql
    assert params == ("abc123",)


def test_set_useful_rejects_invalid_value(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(admin_app, "db", lambda: (_ for _ in ()).throw(
        AssertionError("не мав дійти до БД")
    ))
    response = client.post(
        "/items/abc123/useful",
        data={"value": "maybe", "csrf": _csrf(client)},
        headers=SAME_ORIGIN,
    )
    assert response.status_code == 400


# CSRF-покриття /items/{item_uid}/useful — той самий аргумент, що й для
# /items/{item_uid}/outcome вище: test_every_post_route_is_csrf_covered у
# test_auth.py ітерує app.routes і ловить будь-який новий POST на
# роутері `mutations` без Depends(csrf_guard).
