"""Тести Огляду (переосмислення, задача 6 аудиту 2026-08-11) і читання/
непрочитаного (задача 3):
  - admin.app._leads_badge_context: context processor, що рахує ОДНИМ
    запитом leads_24h і unread_count один раз на рендер шаблону (app.py,
    коментар над реєстрацією в Jinja2Templates) і не має валити сторінку
    при недоступній БД;
  - / (Огляд): 4 плитки-дії, «Needs attention», міні-воронка «This week» —
    dashboard() у admin/app.py;
  - NAV-бейдж біля «Знахідки» покритий у test_items.py (там же й перевірка
    「помилка БД у бейджі не валить /items」) — тут не дублюється.

Без БД і без мережі: `admin.app.db` завжди підмінений monkeypatch'ем. Сесія й
CSRF — ловляться загальними тестами в test_auth.py.
"""

from __future__ import annotations

from admin.tests.test_auth import _login, client  # noqa: E402,F401

from admin import app as admin_app  # noqa: E402


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


# dashboard() робить РІВНО вісім execute() у своєму `with db()`: 0 tiles,
# 1 attention, 2 top_problem_sources, 3 funnel, 4 last_verdict, 5 activity
# (задача 3 аудиту 2026-08-12), 6 top_ecosystems, 7 latest_leads. Дефолтний
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
    # Індекс 8 — контекст-процесор бейджа/плиток (рахується ПІСЛЯ восьми
    # власних запитів dashboard(), бо викликається лише коли
    # templates.TemplateResponse(...) реально рендерить сторінку).
    _login(client)
    _fake_db(
        monkeypatch,
        extra_rows=_dashboard_extra_rows({8: [{"leads_24h": 4, "unread_count": 9}]}),
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
    _login(client)
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

    _login(client)
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
    _login(client)
    _fake_db(monkeypatch, extra_rows=_dashboard_extra_rows())  # index 5 -> []
    html = client.get("/").text
    assert "No activity in the last 14 days" in html


# ── GET /: «Top ecosystems (7d)» (задача 3 аудиту 2026-08-12) ────────────


def test_dashboard_top_ecosystems_widget_renders_bars_and_links(client, monkeypatch):
    _login(client)
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
    _login(client)
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


def test_dashboard_latest_leads_widget_shows_empty_state_without_leads(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch, extra_rows=_dashboard_extra_rows())  # index 7 -> []
    html = client.get("/").text
    assert "No leads yet" in html
