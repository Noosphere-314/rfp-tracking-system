"""Тести Огляду (переосмислення, задача 6 аудиту 2026-08-11) і читання/
непрочитаного (задача 3):
  - admin.app._leads_badge_context: context processor, що рахує ОДНИМ
    запитом leads_24h і unread_count один раз на рендер шаблону (app.py,
    коментар над реєстрацією в Jinja2Templates) і не має валити сторінку
    при недоступній БД;
  - / (Огляд): 4 плитки-дії, «Needs attention», міні-воронка «This week» —
    dashboard() у admin/app.py;
  - NAV-бейдж біля «Знахідки» покритий у test_items.py (там же й перевірка
    「помилка БД у бейджі не валить /items」) — тут не дублюється;
  - Executive «спрощений вигляд» (запит Миколи 2026-08-31): admin.app.
    _view_context і POST /view — сайдбар Executive без cookie rfp_view=full
    показує лише групу Work, з нею — повний нав, як і в будь-кого іншого.

Без БД і без мережі: `admin.app.db` завжди підмінений monkeypatch'ем. Сесія й
CSRF — ловляться загальними тестами в test_auth.py.
"""

from __future__ import annotations

from types import SimpleNamespace

from admin.tests.test_auth import PASSWORD, SAME_ORIGIN, _login, client  # noqa: E402,F401

from admin import app as admin_app  # noqa: E402


def _login_as(client, who: str) -> None:
    """Той самий /login, що й _login() у test_auth.py, але з довільним `who`
    зі списку команди — потрібно перевірити НЕ-Executive підрозділи (Growth
    тощо), а _login() навмисно жорстко зашитий на auth.TEAM[0]."""
    response = client.post(
        "/login",
        data={"password": PASSWORD, "next": "/", "who": who},
        follow_redirects=False,
    )
    assert response.status_code == 303


class _Cursor:
    """Підтримує і fetchall()/fetchone(), і пряму ітерацію (сумісність зі
    старими тестами; dashboard() тепер скрізь явний .fetchone()/.fetchall())."""

    def __init__(self, rows: list[dict]):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class _Conn:
    def __init__(self, sink: list | None = None, extra_rows: dict[int, list[dict]] | None = None):
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
        return _Cursor(self.extra_rows.get(idx, []))

    def commit(self):
        pass


def _fake_db(monkeypatch, sink=None, extra_rows=None):
    conn = _Conn(sink, extra_rows)
    monkeypatch.setattr(admin_app, "db", lambda: conn)
    return conn


# dashboard() робить РІВНО дев'ять execute() у своєму `with db()`: 0 tiles,
# 1 attention, 2 top_problem_sources, 3 funnel, 4 last_verdict, 5 activity
# (задача 3 аудиту 2026-08-12), 6 top_ecosystems, 7 latest_leads,
# 8 closing_deadlines (дедлайн-трекер 2026-08-31). Дефолтний
# набір нижче тримає кожен на «нуль/порожньо», щоб dashboard() не впав
# TypeError на None-рядку (fetchone() на порожньому списку віддає None, а
# `attention[field]` вимагає непорожнього рядка) — тести перевизначають
# лише той індекс, який їм потрібен.
_DASHBOARD_DEFAULTS = {
    0: [{"collected_24h": 0, "briefs_7d": 0}],
    1: [{
        "quarantined_sources": 0, "fetch_failures_24h": 0,
        "pending_stuck": 0, "stale_kb_forums": 0,
    }],
    2: [],
    3: [{"collected": 0, "passed_filter": 0, "leads": 0, "closed": 0}],
    4: [],
    5: [],
    6: [],
    7: [],
    8: [],
}


def _dashboard_extra_rows(overrides: dict[int, list[dict]] | None = None) -> dict[int, list[dict]]:
    merged = dict(_DASHBOARD_DEFAULTS)
    merged.update(overrides or {})
    return merged


# ── _leads_badge_context: юніт-рівень, без клієнта/шаблону ────────────────


def test_leads_badge_context_returns_both_counts_from_the_row(monkeypatch):
    monkeypatch.setattr(
        admin_app, "db",
        lambda: _Conn(extra_rows={0: [{"leads_24h": 12, "unread_count": 5}]}),
    )
    assert admin_app._leads_badge_context(None) == {"leads_24h": 12, "unread_count": 5}


def test_leads_badge_context_defaults_to_zero_when_row_is_missing(monkeypatch):
    """fetchone() повернув None (порожній рядок — не мало б бути в реальній
    БД, але тестовий дублер саме так поводиться за замовчуванням) — бейдж
    все одно 0, а не крах."""
    monkeypatch.setattr(admin_app, "db", lambda: _Conn())
    assert admin_app._leads_badge_context(None) == {"leads_24h": 0, "unread_count": 0}


def test_leads_badge_context_defaults_to_zero_when_db_raises(monkeypatch):
    def boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(admin_app, "db", boom)
    assert admin_app._leads_badge_context(None) == {"leads_24h": 0, "unread_count": 0}


def test_leads_badge_context_query_is_one_combined_select(monkeypatch):
    """Один execute(), не два (задача 3 аудиту 2026-08-11): FILTER-агрегати
    в ОДНОМУ запиті замість двох округлих execute() тримають вартість
    сторінки такою ж, що й до появи unread_count."""
    sink: list = []
    monkeypatch.setattr(
        admin_app, "db",
        lambda: _Conn(sink=sink, extra_rows={0: [{"leads_24h": 1, "unread_count": 1}]}),
    )
    admin_app._leads_badge_context(None)
    assert len(sink) == 1
    sql, params = sink[0]
    assert "seen_items" in sql
    assert "delivered_at > now() - interval '24 hours'" in sql
    assert "viewed_at IS NULL" in sql
    assert params is None


def test_leads_badge_context_unread_reuses_leads24_and_review24_where(monkeypatch):
    """«Переюзай ті самі WHERE-шматки» (задача 3) — не вигадані заново:
    unread_count має посилатись на ті самі фрагменти, що й VIEW_PRESETS."""
    sink: list = []
    monkeypatch.setattr(
        admin_app, "db",
        lambda: _Conn(sink=sink, extra_rows={0: [{"leads_24h": 0, "unread_count": 0}]}),
    )
    admin_app._leads_badge_context(None)
    sql, _ = sink[0]
    assert admin_app._LEADS24_WHERE in sql
    assert admin_app._REVIEW24_WHERE in sql
    assert admin_app.VIEW_PRESETS["leads24"]["where"] == admin_app._LEADS24_WHERE
    assert admin_app.VIEW_PRESETS["review24"]["where"] == admin_app._REVIEW24_WHERE


# ── GET /: плитки-дії ───────────────────────────────────────────────────


def test_dashboard_renders_new_leads_and_unread_tiles(client, monkeypatch):
    # Індекс 9 — контекст-процесор бейджа/плиток (рахується ПІСЛЯ дев'яти
    # власних запитів dashboard() — дев'ятий це closing_deadlines
    # (2026-08-31), — бо викликається лише коли
    # templates.TemplateResponse(...) реально рендерить сторінку).
    _login(client)
    _fake_db(
        monkeypatch,
        extra_rows=_dashboard_extra_rows({9: [{"leads_24h": 4, "unread_count": 9}]}),
    )

    html = client.get("/").text
    assert 'href="/items?view=leads24"' in html
    assert "New leads (24h)" in html
    assert 'class="stat stat--lead"' in html
    assert "<div class=\"stat__num\">4</div>" in html

    assert 'href="/items?view=review24"' in html
    assert "Unread findings" in html
    assert 'class="stat stat--unread"' in html
    assert "<div class=\"stat__num\">9</div>" in html


def test_dashboard_tiles_show_zero_without_crashing(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch, extra_rows=_dashboard_extra_rows())  # index 5 відсутній → 0

    response = client.get("/")
    assert response.status_code == 200
    assert "<div class=\"stat__num\">0</div>" in response.text


def test_dashboard_collected_and_briefs_tiles_link_to_runs_and_briefs(client, monkeypatch):
    _login_as(client, "Growth")  # метрики збору ховаються в Executive-вигляді
    _fake_db(
        monkeypatch,
        extra_rows=_dashboard_extra_rows({0: [{"collected_24h": 42, "briefs_7d": 3}]}),
    )
    html = client.get("/").text
    assert 'href="/runs"' in html
    assert "Collected (24h)" in html
    assert "<div class=\"stat__num\">42</div>" in html
    assert 'href="/briefs"' in html
    assert "Briefs (7d)" in html
    assert "<div class=\"stat__num\">3</div>" in html


# ── GET /: Needs attention ───────────────────────────────────────────────


def test_dashboard_shows_all_normal_empty_state_when_nothing_is_wrong(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch, extra_rows=_dashboard_extra_rows())
    html = client.get("/").text
    assert "All systems normal" in html
    assert "Nothing needs you right now." in html


def test_dashboard_lists_an_issue_row_with_a_link_to_the_action_page(client, monkeypatch):
    _login(client)
    _fake_db(
        monkeypatch,
        extra_rows=_dashboard_extra_rows({1: [{
                "quarantined_sources": 2, "fetch_failures_24h": 0,
                "pending_stuck": 0, "stale_kb_forums": 0,
            }],
        }),
    )
    html = client.get("/").text
    assert "All systems normal" not in html
    assert "2 source(s) in quarantine" in html
    assert 'href="/sources"' in html


def test_dashboard_reports_all_four_kinds_of_issues(client, monkeypatch):
    _login(client)
    _fake_db(
        monkeypatch,
        extra_rows=_dashboard_extra_rows({1: [{
                "quarantined_sources": 1, "fetch_failures_24h": 3,
                "pending_stuck": 5, "stale_kb_forums": 2,
            }],
        }),
    )
    html = client.get("/").text
    assert "1 source(s) in quarantine" in html
    assert 'href="/sources"' in html
    assert "3 source fetch failure(s) in the last 24 hours" in html
    assert 'href="/runs"' in html
    assert "5 finding(s) stuck pending for over 2 hours" in html
    assert 'href="/items?status=pending"' in html
    assert "2 knowledge-base forum(s)" in html
    assert 'href="/kb"' in html


def test_dashboard_shows_top_problem_sources_table_only_when_present(client, monkeypatch):
    _login(client)
    _fake_db(
        monkeypatch,
        extra_rows=_dashboard_extra_rows({1: [{
                "quarantined_sources": 1, "fetch_failures_24h": 0,
                "pending_stuck": 0, "stale_kb_forums": 0,
            }],
            2: [
                {"name": "Flaky Forum", "ecosystem": "Optimism",
                 "consecutive_failures": 7, "quarantined": True},
            ],
        }),
    )
    html = client.get("/").text
    assert "Flaky Forum" in html
    assert "Optimism" in html


# ── GET /: This week funnel ──────────────────────────────────────────────


def test_dashboard_this_week_funnel_numbers_and_links(client, monkeypatch):
    _login(client)
    _fake_db(
        monkeypatch,
        extra_rows=_dashboard_extra_rows({3: [{"collected": 100, "passed_filter": 80, "leads": 12, "closed": 4}],
        }),
    )
    html = client.get("/").text
    assert "This week" in html
    assert 'href="/items?period=7d"' in html
    assert "<span class=\"funnel__num\">100</span>" in html
    assert 'href="/items?period=7d&amp;passed_filter=1"' in html
    assert "<span class=\"funnel__num\">80</span>" in html
    assert 'href="/items?period=7d&amp;delivered=1"' in html
    assert "<span class=\"funnel__num\">12</span>" in html
    assert 'href="/items?period=7d&amp;outcome=closed"' in html
    assert "<span class=\"funnel__num\">4</span>" in html


# ── GET /: сирі таблиці прибрані (задача 6, п. 4) ────────────────────────


def test_dashboard_no_longer_renders_raw_source_health_or_queue_tables(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch, extra_rows=_dashboard_extra_rows())
    html = client.get("/").text
    assert "Source health" not in html
    assert "Queued and stuck" not in html


# ── _bar_pct: чиста функція, бакетування ширини бару (задача 3, 2026-08-12) ──


def test_bar_pct_buckets_to_the_nearest_five():
    assert admin_app._bar_pct(3, 10) == 30       # 30% — точно на кроці
    assert admin_app._bar_pct(1, 3) == 35         # 33.3% -> найближчі 35
    assert admin_app._bar_pct(10, 10) == 100      # максимум = повний бар
    assert admin_app._bar_pct(0, 10) == 0


def test_bar_pct_zero_or_negative_max_value_returns_zero_without_crashing():
    assert admin_app._bar_pct(5, 0) == 0
    assert admin_app._bar_pct(5, -1) == 0


# ── GET /: «Activity, last 14 days» (задача 3 аудиту 2026-08-12) ─────────


def test_dashboard_activity_widget_renders_bucketed_bar_width_classes(client, monkeypatch):
    from datetime import date

    _login_as(client, "Growth")  # метрики збору ховаються в Executive-вигляді
    activity_rows = [
        {"day": date(2026, 8, 1), "collected": 10, "leads": 2},
        {"day": date(2026, 8, 2), "collected": 5, "leads": 5},
    ]
    _fake_db(monkeypatch, extra_rows=_dashboard_extra_rows({5: activity_rows}))

    html = client.get("/").text
    assert "Activity, last 14 days" in html
    assert "08-01" in html and "08-02" in html
    # max_collected=10 (день1=100%,день2=50%); max_leads=5 (день1=40%,день2=100%)
    assert 'class="bar__fill w-100"' in html
    assert 'class="bar__fill w-50"' in html
    assert 'class="bar__fill bar__fill--lead w-40"' in html
    assert 'class="bar__fill bar__fill--lead w-100"' in html


def test_dashboard_activity_widget_shows_empty_state_when_nothing_collected(client, monkeypatch):
    _login_as(client, "Growth")  # метрики збору ховаються в Executive-вигляді
    _fake_db(monkeypatch, extra_rows=_dashboard_extra_rows())  # index 5 -> []
    html = client.get("/").text
    assert "No activity in the last 14 days" in html


# ── GET /: «Top ecosystems (7d)» (задача 3 аудиту 2026-08-12) ────────────


def test_dashboard_top_ecosystems_widget_renders_bars_and_links(client, monkeypatch):
    _login_as(client, "Growth")  # метрики збору ховаються в Executive-вигляді
    eco_rows = [
        {"ecosystem": "Optimism", "n": 10},
        {"ecosystem": "Arbitrum", "n": 5},
    ]
    _fake_db(monkeypatch, extra_rows=_dashboard_extra_rows({6: eco_rows}))

    html = client.get("/").text
    assert "Top ecosystems (7d)" in html
    assert 'href="/items?ecosystem=Optimism&amp;period=7d"' in html
    assert 'class="bar__fill w-100"' in html  # Optimism, максимум
    assert 'class="bar__fill w-50"' in html   # Arbitrum, половина максимуму


def test_dashboard_top_ecosystems_widget_shows_empty_state_without_findings(client, monkeypatch):
    _login_as(client, "Growth")  # метрики збору ховаються в Executive-вигляді
    _fake_db(monkeypatch, extra_rows=_dashboard_extra_rows())  # index 6 -> []
    html = client.get("/").text
    assert "No findings in the last 7 days" in html


# ── GET /: «Latest leads» (задача 3 аудиту 2026-08-12) ───────────────────


def test_dashboard_latest_leads_widget_renders_rows_with_brief_link(client, monkeypatch):
    from datetime import datetime, timezone

    _login(client)
    leads_rows = [{
        "item_uid": "uid-1", "title": "Fund an oracle relayer",
        "url": "https://example.test/rfp/1",
        "delivered_at": datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc),
        "source_ecosystem": "Optimism", "brief_id": 7,
    }]
    _fake_db(monkeypatch, extra_rows=_dashboard_extra_rows({7: leads_rows}))

    html = client.get("/").text
    assert "Latest leads" in html
    assert "Fund an oracle relayer" in html
    assert 'href="https://example.test/rfp/1"' in html
    assert 'href="/briefs/7"' in html
    assert 'href="/items?view=leads24"' in html


# ── Executive «спрощений вигляд»: _view_context (юніт, без клієнта) ──────
#
# auth.session_who підмінений напряму на admin_app.auth (той самий модуль,
# на який посилається admin.app через `from admin import auth`) — швидше й
# точковіше, ніж проганяти справжній підписаний cookie заради трьох гілок
# чистої функції.


def test_view_context_executive_without_full_cookie_is_simple(monkeypatch):
    monkeypatch.setattr(admin_app.auth, "session_who", lambda request: "Executive")
    request = SimpleNamespace(cookies={})
    assert admin_app._view_context(request) == {"simple_view": True, "show_view_toggle": True}


def test_view_context_executive_with_full_cookie_is_not_simple(monkeypatch):
    monkeypatch.setattr(admin_app.auth, "session_who", lambda request: "Executive")
    request = SimpleNamespace(cookies={admin_app.VIEW_COOKIE: "full"})
    assert admin_app._view_context(request) == {"simple_view": False, "show_view_toggle": True}


def test_view_context_non_executive_never_simple_and_never_shows_toggle(monkeypatch):
    """Навіть якщо в НЕ-Executive чомусь опиниться rfp_view=full (напр.,
    людина була Executive і поле команди змінили) — це нічого не змінює:
    simple_view рахується ЛИШЕ для Executive, а toggle ховається геть."""
    monkeypatch.setattr(admin_app.auth, "session_who", lambda request: "Growth")
    assert admin_app._view_context(SimpleNamespace(cookies={})) == {
        "simple_view": False, "show_view_toggle": False,
    }
    assert admin_app._view_context(SimpleNamespace(cookies={admin_app.VIEW_COOKIE: "full"})) == {
        "simple_view": False, "show_view_toggle": False,
    }


# ── Executive «спрощений вигляд»: рендер сайдбару в base.html ───────────


def test_executive_without_view_cookie_sees_only_work_group_in_sidebar(client, monkeypatch):
    _login(client)  # WHO = auth.TEAM[0] = "Executive"
    _fake_db(monkeypatch, extra_rows=_dashboard_extra_rows())
    html = client.get("/").text

    # Work лишається повністю — і Overview (активний пункт), і решта.
    assert 'href="/"' in html
    assert 'href="/items"' in html
    assert 'href="/briefs"' in html
    assert 'href="/kb"' in html
    assert 'href="/chat"' in html

    # Configuration/System/External — приховані разом зі своїми пунктами.
    assert 'href="/sources"' not in html
    assert 'href="/keywords"' not in html
    assert 'href="/settings"' not in html
    assert "Configuration" not in html
    assert "System" not in html
    assert "External" not in html

    # Ескейп-люк на місці.
    assert 'action="/view"' in html
    assert "Full version" in html


def test_executive_with_view_cookie_full_sees_the_complete_sidebar(client, monkeypatch):
    _login(client)
    client.cookies.set(admin_app.VIEW_COOKIE, "full")
    _fake_db(monkeypatch, extra_rows=_dashboard_extra_rows())
    html = client.get("/").text

    assert 'href="/sources"' in html
    assert 'href="/keywords"' in html
    assert 'href="/settings"' in html
    assert 'href="/runs"' in html
    assert "External" in html  # N8N_URL заданий у test_auth.py

    # Кнопка лишається, але тепер пропонує зворотний напрямок.
    assert 'action="/view"' in html
    assert "Simple view" in html


def test_non_executive_sees_full_sidebar_and_no_toggle_button(client, monkeypatch):
    _login_as(client, "Growth")
    _fake_db(monkeypatch, extra_rows=_dashboard_extra_rows())
    html = client.get("/").text

    assert 'href="/sources"' in html
    assert 'href="/runs"' in html
    assert "External" in html
    assert 'action="/view"' not in html
    assert "Full version" not in html
    assert "Simple view" not in html


# ── Executive «спрощений вигляд»: POST /view ─────────────────────────────


def test_view_toggle_post_sets_the_cookie_and_redirects_to_next(client):
    _login(client)
    csrf = admin_app.auth.csrf_for(client.cookies[admin_app.auth.COOKIE_BASE])
    response = client.post(
        "/view",
        data={"csrf": csrf, "view": "full", "next": "/items"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/items"
    assert response.cookies.get(admin_app.VIEW_COOKIE) == "full"


def test_view_toggle_post_back_to_simple_clears_the_cookie(client):
    _login(client)
    client.cookies.set(admin_app.VIEW_COOKIE, "full")
    csrf = admin_app.auth.csrf_for(client.cookies[admin_app.auth.COOKIE_BASE])
    response = client.post(
        "/view",
        data={"csrf": csrf, "view": "", "next": "/"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    # Cookie видалена (delete_cookie): або відсутня в наступному запиті,
    # або пуста — обидва варіанти означають «більше не full».
    assert admin_app.VIEW_COOKIE not in response.cookies or not response.cookies.get(
        admin_app.VIEW_COOKIE
    )


def test_view_toggle_post_with_malicious_next_falls_back_to_root(client):
    """Той самий open-redirect ризик, що й /lang і /login (auth.safe_next):
    протокольний і protocol-relative `next` не мають нікуди відпускати."""
    _login(client)
    csrf = admin_app.auth.csrf_for(client.cookies[admin_app.auth.COOKIE_BASE])
    for bad_next in ("https://evil.com", "//evil.com"):
        response = client.post(
            "/view",
            data={"csrf": csrf, "view": "full", "next": bad_next},
            headers=SAME_ORIGIN,
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/", bad_next


def test_dashboard_latest_leads_widget_shows_empty_state_without_leads(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch, extra_rows=_dashboard_extra_rows())  # index 7 -> []
    html = client.get("/").text
    assert "No leads yet" in html


# ── Closing soon: дедлайн-трекер на Overview (2026-08-31) ─────────────────


def test_dashboard_renders_closing_deadlines_with_dismiss(client, monkeypatch):
    import datetime

    _login(client)
    _fake_db(monkeypatch, extra_rows=_dashboard_extra_rows({8: [{
        "id": 3, "title": "ENS SPP3 Marketplace RFP", "ecosystem": "ENS",
        "deadline": datetime.date(2026, 9, 2), "url": "https://x/t/1",
        "days_left": 2,
    }]}))

    html = client.get("/").text
    assert "Closing soon" in html
    assert "ENS SPP3 Marketplace RFP" in html
    # ≤3 днів — warn-бейдж (той самий поріг, що пінгує digest у Telegram).
    assert 'class="badge b-warn"' in html
    assert 'action="/deadlines/3/dismiss"' in html


def test_dashboard_hides_the_panel_when_no_deadlines(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch, extra_rows=_dashboard_extra_rows())
    assert "Closing soon" not in client.get("/").text


def test_dismiss_deadline_updates_and_redirects(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, sink=sink, extra_rows=_dashboard_extra_rows())
    from admin import auth as auth_mod
    r = client.post("/deadlines/3/dismiss",
                    data={"csrf": auth_mod.csrf_for(client.cookies[auth_mod.COOKIE_BASE])},
                    headers=SAME_ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert any("SET dismissed_at = now()" in sql for sql, _ in sink)


def test_executive_overview_drops_collection_metrics(client, monkeypatch):
    """План 2026-08-31, дизайн п.2: метрики збору — шум для СЕО. У
    спрощеному вигляді їх у розмітці НЕМАЄ ЗОВСІМ (а не сховані
    атрибутом: тоді тести «бачили» б текст, якого людина не бачить)."""
    _login_as(client, "Executive")
    _fake_db(monkeypatch, extra_rows=_dashboard_extra_rows())

    html = client.get("/").text
    assert "Collected (24h)" not in html
    assert "Activity, last 14 days" not in html
    assert "Top ecosystems" not in html
    # CEO-блоки лишаються.
    assert "Needs attention" in html
    assert "This week" in html


def test_executive_full_version_restores_collection_metrics(client, monkeypatch):
    """Ескейп-люк «Full version» (вимога Миколи) повертає повний вигляд."""
    _login_as(client, "Executive")
    client.cookies.set("rfp_view", "full")
    _fake_db(monkeypatch, extra_rows=_dashboard_extra_rows())

    html = client.get("/").text
    assert "Collected (24h)" in html
    assert "Activity, last 14 days" in html
