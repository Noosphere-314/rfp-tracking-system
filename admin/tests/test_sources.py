"""Тести /sources/add — задача «менше ручного JSON» (2026-08-12): кнопка
Discover для discourse і автозбір config із позначених чекбоксів (cats).

Без БД і без мережі: `admin.app.db` підмінений тим самим прийомом «чергою
execute() за індексом», що й у test_runs.py. Живий тест-фетч (`_test_fetch`)
підмінений окремо. Для Discover мережу зображує підміна класу `HttpClient` —
`_discover_discourse` (admin/app.py) відкриває `with db() as conn, HttpClient(conn)
as client:` рівно як `_test_fetch`, тож фальшивий клієнт повністю ігнорує
conn і жодного реального SQL/HTTP не робить.

Імпорт test_auth ПЕРШИМ — env виставляється до першого імпорту admin.app (той
самий трюк, що й в інших тестах admin/tests/).
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
    """Той самий фейк, що й admin/tests/test_runs.py: черга execute() за
    індексом виклику, sink записує (sql, params) для перевірки."""

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


def _post_add(client, **fields):
    data = {"csrf": _csrf(client)}
    data.update(fields)
    return client.post(
        "/sources/add", data=data, headers=SAME_ORIGIN, follow_redirects=False
    )


# ── Фейковий HttpClient для Discover — жодної мережі, жодного netguard ────


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHttpClient:
    """Підміна worker.http.HttpClient: конструктор ігнорує conn, .get()
    повертає заготований payload (або кидає підготовану помилку) — жодного
    httpx/netguard виклику. `admin_app.HttpClient` — той самий символ, який
    `_discover_discourse`/`_test_fetch` викликають напряму."""

    payload: dict | None = None
    error: Exception | None = None

    def __init__(self, conn):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, *, headers=None, use_cache=True):
        if self.error is not None:
            raise self.error
        return _FakeResponse(self.payload)


def _fake_http_client(monkeypatch, *, payload=None, error=None):
    cls = type("_FakeHttpClientInstance", (_FakeHttpClient,), {"payload": payload, "error": error})
    monkeypatch.setattr(admin_app, "HttpClient", cls)
    return cls


DISCOURSE_PAYLOAD = {
    "category_list": {
        "categories": [
            {
                "id": 5, "slug": "grants", "topic_count": 12,
                "subcategory_list": [
                    {"id": 51, "slug": "gov-fund-missions", "topic_count": 30},
                ],
            },
            {"id": 7, "slug": "governance", "topic_count": 40},
        ]
    }
}


# ── Discover: рендер чекбоксів ─────────────────────────────────────────────


def test_discover_renders_checkboxes_for_found_categories(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch)
    _fake_http_client(monkeypatch, payload=DISCOURSE_PAYLOAD)

    response = _post_add(
        client,
        action="discover", type="discourse", name="", ecosystem="",
        url="https://forum.example.org", category="", lane="rfp", config="{}",
    )

    assert response.status_code == 200
    body = response.text
    # Топ-рівень — спаданням за topic_count: governance(40) перед grants(12).
    assert body.index('value="7:governance"') < body.index('value="5:grants"')
    # Підкатегорія йде одразу під своїм батьком, а не змішується в загальний
    # топ-рівневий порядок (у неї самої topic_count=30, вище за grants=12).
    assert body.index('value="5:grants"') < body.index('value="51:gov-fund-missions"')
    assert "Found 3 categories" in body


def test_discover_non_discourse_type_shows_clear_error(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch)

    response = _post_add(
        client,
        action="discover", type="rss", name="", ecosystem="",
        url="https://example.org/feed", category="", lane="rfp", config="{}",
    )

    assert response.status_code == 400
    assert "Discover works for discourse sources only" in response.text


def test_discover_network_error_renders_form_not_500(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch)
    _fake_http_client(monkeypatch, error=RuntimeError("connection refused"))

    response = _post_add(
        client,
        action="discover", type="discourse", name="", ecosystem="",
        url="https://forum.example.org", category="", lane="rfp", config="{}",
    )

    assert response.status_code == 400
    assert response.status_code != 500
    assert "Discover failed" in response.text


def test_discover_empty_category_list_is_a_form_error_not_a_crash(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch)
    _fake_http_client(monkeypatch, payload={"category_list": {"categories": []}})

    response = _post_add(
        client,
        action="discover", type="discourse", name="", ecosystem="",
        url="https://forum.example.org", category="", lane="rfp", config="{}",
    )

    assert response.status_code == 400
    assert "No categories found" in response.text


# ── Save: автозбір config із cats[] ─────────────────────────────────────────


def test_save_with_cats_and_empty_config_builds_categories_config(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch)
    captured = []

    def fake_test_fetch(row):
        captured.append(row)
        return 1, ""

    monkeypatch.setattr(admin_app, "_test_fetch", fake_test_fetch)

    response = _post_add(
        client,
        action="save", type="discourse", name="Test forum", ecosystem="Test",
        url="https://forum.example.org", category="", lane="rfp", config="{}",
        cats=["7:governance", "5:grants"],
    )

    assert response.status_code == 303, response.text
    assert len(captured) == 1
    assert captured[0]["config"] == {
        "categories": [{"slug": "governance", "id": 7}, {"slug": "grants", "id": 5}]
    }


def test_manual_config_has_priority_over_cats(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch)
    captured = []

    def fake_test_fetch(row):
        captured.append(row)
        return 1, ""

    monkeypatch.setattr(admin_app, "_test_fetch", fake_test_fetch)

    response = _post_add(
        client,
        action="save", type="discourse", name="Test forum", ecosystem="Test",
        url="https://forum.example.org", category="", lane="rfp",
        config='{"categories": [{"slug": "manual", "id": 99}]}',
        cats=["7:governance"],
    )

    assert response.status_code == 303, response.text
    assert len(captured) == 1
    assert captured[0]["config"] == {"categories": [{"slug": "manual", "id": 99}]}


def test_invalid_cats_are_dropped_and_capped_at_forty():
    valid = [f"{i}:cat-{i}" for i in range(50)]
    invalid = ["not-a-pair", "abc:slug", "1:", "1:UPPER", ":5"]

    result = admin_app._cats_to_config(invalid + valid)

    assert len(result["categories"]) == 40
    # Невалідні (кинуті перед валідними у списку вище) не потрапили в результат.
    assert all(c["slug"].startswith("cat-") for c in result["categories"])
