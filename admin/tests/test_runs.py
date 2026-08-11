"""Тести /runs — пояснювальна панель, серверний errors-фільтр і «Test all
sources now» (задача 6 аудиту 2026-08-12).

Без БД і без мережі: `admin.app.db` підмінений monkeypatch'ем (той самий
прийом «черга execute() за індексом», що й у test_dashboard.py/test_items.py
— один об'єкт _Conn на весь тест, `_call_index` рахує КОЖЕН execute() підряд,
незалежно від того, скільки разів хендлер відкриває `with db()`). Живий
тест-фетч (`_test_fetch`) підмінений окремо — жодного реального HTTP і
жодного очікування netguard.

Імпорт test_auth ПЕРШИМ — env (DASHBOARD_PASSWORD, SESSION_SECRET, …)
виставляється до першого імпорту admin.app (той самий трюк, що й в інших
тестах admin/tests/).
"""

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


def _csrf(client) -> str:
    return auth.csrf_for(client.cookies[auth.COOKIE_BASE])


# ── GET /runs: errors-фільтр (задача 6, п.2) ──────────────────────────────


def test_runs_default_has_no_where_clause(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, sink=sink)

    response = client.get("/runs")
    assert response.status_code == 200
    sql, params = sink[0]
    assert "WHERE" not in sql
    assert params == ()


def test_runs_errors_filter_adds_has_errors_where(client, monkeypatch):
    """WHERE тут — той самий критерій, що вже позначає data-fail на рядку
    (sources_failed > 0 АБО непорожній detail->'failures')."""
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, sink=sink)

    response = client.get("/runs", params={"errors": "1"})
    assert response.status_code == 200
    sql, params = sink[0]
    assert "sources_failed > 0" in sql
    assert "jsonb_array_length" in sql
    assert params == ()


def test_runs_mode_and_errors_filters_combine_with_and(client, monkeypatch):
    _login(client)
    sink: list = []
    _fake_db(monkeypatch, sink=sink)

    response = client.get("/runs", params={"mode": "run", "errors": "1"})
    assert response.status_code == 200
    sql, params = sink[0]
    assert "mode = %s" in sql
    assert "sources_failed > 0" in sql
    assert " AND " in sql
    assert params == ("run",)


def test_runs_errors_checkbox_reflects_url_state(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch)

    html = client.get("/runs", params={"errors": "1"}).text
    assert 'name="errors" value="1" checked' in html


def test_runs_errors_checkbox_unchecked_by_default(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch)

    html = client.get("/runs").text
    assert 'name="errors" value="1" checked' not in html


# ── GET /runs: пояснювальна панель (задача 6, п.1) ────────────────────────


def test_runs_page_shows_explanatory_banner(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch)

    html = client.get("/runs").text
    assert "Each row is one collection run" in html
    assert "Needs attention" in html


# ── POST /runs/test-sources (задача 6, п.3) ───────────────────────────────


def test_test_all_sources_reuses_test_fetch_and_reports_results(client, monkeypatch):
    """`_test_all_sources` мусить ПЕРЕВИКОРИСТОВУВАТИ `_test_fetch` (той
    самий живий тест-фетч, що й add_source/Sources) — не власну копію
    HTTP-логіки. Тут це перевірено підміною `_test_fetch` і спостереженням
    за тим, що саме вона й була викликана для кожного увімкненого джерела."""
    _login(client)

    calls = []

    def fake_test_fetch(row):
        calls.append(row["name"])
        if row["name"] == "Broken Forum":
            return 0, "boom"
        return 3, ""

    monkeypatch.setattr(admin_app, "_test_fetch", fake_test_fetch)

    sources_rows = [
        {"id": 1, "type": "discourse", "name": "Good Forum", "ecosystem": "Optimism",
         "url": "https://good.test", "category": None, "config": {}, "lane": "rfp"},
        {"id": 2, "type": "discourse", "name": "Broken Forum", "ecosystem": "Arbitrum",
         "url": "https://bad.test", "category": None, "config": {}, "lane": "rfp"},
    ]
    _fake_db(monkeypatch, extra_rows={0: sources_rows, 1: [], 2: []})

    response = client.post(
        "/runs/test-sources", data={"csrf": _csrf(client)}, headers=SAME_ORIGIN,
    )
    assert response.status_code == 200
    assert calls == ["Good Forum", "Broken Forum"]
    assert "Source test results" in response.text
    assert "Good Forum" in response.text
    assert "Broken Forum" in response.text
    assert "boom" in response.text


def test_test_all_sources_selects_only_enabled_sources(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(admin_app, "_test_fetch", lambda row: (1, ""))
    sink: list = []
    _fake_db(monkeypatch, sink=sink, extra_rows={0: [], 1: [], 2: []})

    client.post("/runs/test-sources", data={"csrf": _csrf(client)}, headers=SAME_ORIGIN)
    sql, _ = sink[0]
    assert "WHERE enabled" in sql


def test_test_all_sources_no_enabled_sources_shows_empty_message(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(
        admin_app, "_test_fetch",
        lambda row: (_ for _ in ()).throw(AssertionError("не мав викликатись")),
    )
    _fake_db(monkeypatch, extra_rows={0: [], 1: [], 2: []})

    response = client.post(
        "/runs/test-sources", data={"csrf": _csrf(client)}, headers=SAME_ORIGIN
    )
    assert response.status_code == 200
    assert "No enabled sources to test." in response.text


def test_test_all_sources_reports_timeout_without_crashing(client, monkeypatch):
    """Джерело, що не встигло за 5с, не валить всю перевірку — рядок
    результату отримує `ok=False` з поясненням «timed out», а решта джерел
    (тут — жодного далі) все одно перевіряється. Таймаут імітований підміною
    ЛИШЕ `concurrent.futures.ThreadPoolExecutor` (не глобального класу
    `Future` — той під капотом використовує і сам TestClient/anyio для
    власної плинки, і підміна його `.result()` валить інфраструктуру тесту,
    а не лише код під тестом), а не реальним сном на 5с — тест мусить
    лишатись швидким."""
    import concurrent.futures

    _login(client)
    monkeypatch.setattr(admin_app, "_test_fetch", lambda row: (1, ""))

    class _FakeFuture:
        def result(self, timeout=None):
            raise concurrent.futures.TimeoutError()

    class _FakeExecutor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def submit(self, fn, *args, **kwargs):
            return _FakeFuture()

    monkeypatch.setattr(
        concurrent.futures, "ThreadPoolExecutor", lambda max_workers=1: _FakeExecutor()
    )

    sources_rows = [{
        "id": 1, "type": "discourse", "name": "Slow Forum", "ecosystem": "Optimism",
        "url": "https://slow.test", "category": None, "config": {}, "lane": "rfp",
    }]
    _fake_db(monkeypatch, extra_rows={0: sources_rows, 1: [], 2: []})

    response = client.post(
        "/runs/test-sources", data={"csrf": _csrf(client)}, headers=SAME_ORIGIN
    )
    assert response.status_code == 200
    assert "Slow Forum" in response.text
    assert "timed out after 5s" in response.text


# CSRF-покриття /runs/test-sources НЕ дублюється окремим тестом тут:
# test_every_post_route_is_csrf_covered у test_auth.py вже ітерує app.routes
# і ловить будь-який новий POST на роутері `mutations` без
# Depends(csrf_guard) — той самий аргумент, що й у test_items.py.
