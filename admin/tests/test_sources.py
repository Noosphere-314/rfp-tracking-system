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

import json as _json

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
    """Той самий контракт, що й worker.http.Response: `.text` — сирий рядок,
    `.json()` — `json.loads(self.text)` (задача «Detect type», 2026-08-12,
    потребує і .text для HTML-тіла/RSS-регексу, і .json() для discourse-
    перевірки — обидва з тим самим payload-джерелом, як у продакшн-коді)."""

    def __init__(self, *, text: str = "", payload=None):
        self.text = _json.dumps(payload) if payload is not None else text

    def json(self):
        return _json.loads(self.text)


class _FakeHttpClient:
    """Підміна worker.http.HttpClient: конструктор ігнорує conn, .get()
    повертає заготований payload/text (або кидає підготовану помилку) —
    жодного httpx/netguard виклику. `admin_app.HttpClient` — той самий
    символ, який `_discover_discourse`/`_detect_source_type`/`_test_fetch`
    викликають напряму. Той самий фейк іде на КОЖЕН .get() у межах одного
    тесту (Detect type може зробити до двох викликів — about.json, тоді
    корінь для RSS) — досить для всіх сценаріїв нижче, бо або перший виклик
    вже дає відповідь, або друга спроба дивиться на те саме тіло іншим
    патерном (RSS-лінк у тому ж HTML, що не є Discourse JSON)."""

    payload: dict | None = None
    text: str = ""
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
        return _FakeResponse(text=self.text, payload=self.payload)


def _fake_http_client(monkeypatch, *, payload=None, text="", error=None):
    cls = type(
        "_FakeHttpClientInstance", (_FakeHttpClient,),
        {"payload": payload, "text": text, "error": error},
    )
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


# ── Discover: людська помилка на не-Discourse HTML (задача 2) ─────────────


def test_discover_html_response_shows_human_error_not_jsondecodeerror(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch)
    _fake_http_client(
        monkeypatch, text="<!doctype html><html><body><div id=root></div></body></html>",
    )

    response = _post_add(
        client,
        action="discover", type="discourse", name="", ecosystem="",
        url="https://ethereum.forum", category="", lane="rfp", config="{}",
    )

    assert response.status_code == 400
    assert "JSONDecodeError" not in response.text
    assert "ethereum.forum" in response.text
    assert "not a Discourse forum" in response.text
    assert "Detect type" in response.text


# ── Detect type (задача 1) ──────────────────────────────────────────────


def test_detect_discourse_with_stats(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch)
    _fake_http_client(
        monkeypatch,
        # topicS_count/postS_count — реальні імена ключів Discourse
        # (перевірено на ethereum-magicians.org 2026-08-12).
        payload={"about": {"stats": {"topics_count": 120, "posts_count": 900}}},
    )

    response = _post_add(
        client,
        action="detect", type="rss", name="", ecosystem="",
        url="https://forum.example.org", category="", lane="rfp", config="{}",
    )

    assert response.status_code == 200, response.text
    assert "120" in response.text
    assert "900" in response.text
    # <select name=type> перемкнувся на discourse: саме цей <option> тепер
    # selected (Jinja екранує лапки всередині атрибута як &#34;).
    assert "data-config-tpl='{&#34;categories&#34;: []}' selected" in response.text


def test_detect_html_instead_of_json_has_clear_error(client, monkeypatch):
    """Той самий кейс власника, що й у Discover (задача 2): ethereum.forum —
    SPA, що на будь-який шлях віддає HTML зі статусом 200."""
    _login(client)
    _fake_db(monkeypatch)
    _fake_http_client(
        monkeypatch, text="<!doctype html><html><body><div id=root></div></body></html>",
    )

    response = _post_add(
        client,
        action="detect", type="rss", name="", ecosystem="",
        url="https://ethereum.forum", category="", lane="rfp", config="{}",
    )

    assert response.status_code != 500
    assert "JSONDecodeError" not in response.text
    assert "ethereum.forum" in response.text


def test_detect_github_repo(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch)

    response = _post_add(
        client,
        action="detect", type="rss", name="", ecosystem="",
        url="https://github.com/filecoin-project/community", category="", lane="rfp", config="{}",
    )

    assert response.status_code == 200, response.text
    assert "filecoin-project/community" in response.text
    # Формат мусить збігатися з worker/fetchers/github_discussions.py
    # (repo.get("owner")/.get("name")) — інакше форма підказує конфіг,
    # який фетчер відкидає як "malformed repo entry".
    assert "&#34;owner&#34;: &#34;owner&#34;" in response.text


def test_detect_snapshot(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch)

    response = _post_add(
        client,
        action="detect", type="rss", name="", ecosystem="",
        url="https://hub.snapshot.org", category="", lane="rfp", config="{}",
    )

    assert response.status_code == 200, response.text
    assert "spaces" in response.text
    assert "data-config-tpl='{&#34;spaces&#34;: [&#34;example.eth&#34;]}' selected" in response.text


def test_detect_rss_feed_found(client, monkeypatch):
    html = (
        '<html><head>'
        '<link rel="alternate" type="application/rss+xml" href="/feed.xml">'
        '</head></html>'
    )
    _login(client)
    _fake_db(monkeypatch)
    _fake_http_client(monkeypatch, text=html)

    response = _post_add(
        client,
        action="detect", type="discourse", name="", ecosystem="",
        url="https://blog.example.org", category="", lane="rfp", config="{}",
    )

    assert response.status_code == 200, response.text
    # Відносний href абсолютизований відносно origin.
    assert "https://blog.example.org/feed.xml" in response.text
    assert "data-config-tpl='{}' selected" in response.text


def test_detect_nothing_found_is_a_human_error(client, monkeypatch):
    _login(client)
    _fake_db(monkeypatch)
    _fake_http_client(monkeypatch, text="<html><body>Just a static page</body></html>")

    response = _post_add(
        client,
        action="detect", type="rss", name="", ecosystem="",
        url="https://staticsite.example", category="", lane="rfp", config="{}",
    )

    assert response.status_code == 400
    assert "staticsite.example" in response.text
    assert "JSONDecodeError" not in response.text


# ── Автошаблон типу vs позначені категорії (баг gov.uniswap.org) ──────


def test_empty_type_template_does_not_block_discovered_cats(client, monkeypatch):
    """app.js підставляє {"categories": []} у порожню textarea при виборі
    типу. Раніше `not config_obj` вважав це «ручним конфігом» і мовчки
    ігнорував позначені чекбокси — Test and save падав сирим
    «needs config.categories» (знайдено на живому gov.uniswap.org)."""
    _login(client)
    _fake_db(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        admin_app, "_test_fetch",
        lambda row: (captured.update(row) or (3, "")),
    )

    response = _post_add(
        client, action="save", type="discourse", name="Uniswap",
        ecosystem="Uniswap", url="https://gov.uniswap.org", category="",
        lane="rfp", config='{"categories": []}', cats=["8:governance"],
    )

    assert response.status_code in (200, 303)
    assert captured["config"] == {"categories": [{"slug": "governance", "id": 8}]}


def test_discourse_without_categories_gets_a_human_error(client, monkeypatch):
    """Порожній discourse-конфіг ловиться ДО мережі: людині кажуть натиснути
    Discover, а не показують технічний ValueError фетчера."""
    _login(client)
    _fake_db(monkeypatch)
    monkeypatch.setattr(
        admin_app, "_test_fetch",
        lambda row: (_ for _ in ()).throw(AssertionError("must not fetch")),
    )

    response = _post_add(
        client, action="save", type="discourse", name="Uniswap",
        ecosystem="Uniswap", url="https://gov.uniswap.org", category="",
        lane="rfp", config='{"categories": []}',
    )

    assert response.status_code == 400
    assert "Discover categories" in response.text
    assert "ValueError" not in response.text
