"""Тести розділу B — «Ліди видимі в дашборді»:
  - admin.app._leads_badge_context: context processor, що рахує leads_24h
    один раз на рендер шаблону (app.py, коментар над реєстрацією в
    Jinja2Templates) і не має валити сторінку при недоступній БД;
  - / (Огляд): плитка «New leads (24h)» — переконструйована з того самого
    `leads_24h`, без окремого SELECT у самому dashboard();
  - NAV-бейдж біля «Знахідки» покритий у test_items.py (там же й перевірка
    「помилка БД у бейджі не валить /items」) — тут не дублюється.

Без БД і без мережі: `admin.app.db` завжди підмінений monkeypatch'ем. Сесія й
CSRF — ловляться загальними тестами в test_auth.py.
"""

from __future__ import annotations

from admin.tests.test_auth import _login, client  # noqa: E402,F401

from admin import app as admin_app  # noqa: E402


class _Cursor:
    """Підтримує і fetchall()/fetchone(), і пряму ітерацію (`for row in
    conn.execute(...)`) — dashboard() рахує status_counts саме так, на
    відміну від решти хендлерів, де завжди явний .fetchall()."""

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


# ── _leads_badge_context: юніт-рівень, без клієнта/шаблону ────────────────


def test_leads_badge_context_returns_the_count_from_the_row(monkeypatch):
    monkeypatch.setattr(admin_app, "db", lambda: _Conn(extra_rows={0: [{"n": 12}]}))
    assert admin_app._leads_badge_context(None) == {"leads_24h": 12}


def test_leads_badge_context_defaults_to_zero_when_row_is_missing(monkeypatch):
    """fetchone() повернув None (порожній COUNT — не мало б бути в реальній
    БД, але тестовий дублер саме так поводиться за замовчуванням) — бейдж
    все одно 0, а не крах."""
    monkeypatch.setattr(admin_app, "db", lambda: _Conn())
    assert admin_app._leads_badge_context(None) == {"leads_24h": 0}


def test_leads_badge_context_defaults_to_zero_when_db_raises(monkeypatch):
    def boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(admin_app, "db", boom)
    assert admin_app._leads_badge_context(None) == {"leads_24h": 0}


def test_leads_badge_context_query_counts_delivered_in_last_24h(monkeypatch):
    sink: list = []
    monkeypatch.setattr(admin_app, "db", lambda: _Conn(sink=sink, extra_rows={0: [{"n": 1}]}))
    admin_app._leads_badge_context(None)
    sql, params = sink[0]
    assert "seen_items" in sql
    assert "delivered_at > now() - interval '24 hours'" in sql
    assert params is None


# ── GET /: плитка «New leads (24h)» ────────────────────────────────────────


def test_dashboard_renders_new_leads_tile_linking_to_leads24_view(client, monkeypatch):
    # Індекс 3 — sources_total (dashboard() робить .fetchone()["n"] без
    # захисту від None, бо COUNT(*) завжди повертає рівно один рядок у
    # справжній БД); індекс 6 — контекст-процесор бейджа/плитки лідів
    # (рахується ПІСЛЯ власних 6 запитів dashboard(), бо викликається лише
    # коли templates.TemplateResponse(...) реально рендерить сторінку).
    _login(client)
    _fake_db(monkeypatch, extra_rows={3: [{"n": 0}], 6: [{"n": 4}]})

    html = client.get("/").text
    assert 'href="/items?view=leads24"' in html
    assert "New leads (24h)" in html
    assert 'class="stat stat--lead"' in html
    # Те саме число, що дав би NAV-бейдж — джерело даних спільне.
    assert "<div class=\"stat__num\">4</div>" in html


def test_dashboard_leads_tile_shows_zero_without_crashing(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch, extra_rows={3: [{"n": 0}]})  # index 6 відсутній → 0

    response = client.get("/")
    assert response.status_code == 200
    assert "<div class=\"stat__num\">0</div>" in response.text
