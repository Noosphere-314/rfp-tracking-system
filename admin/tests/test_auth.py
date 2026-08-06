"""Сім тестів авторизації дашборда (специфікація «дашборд v2», розділ 5.7).

Тестів на admin-роути в репозиторії не було взагалі, а авторизація ламає все
або нічого. Два перші тести ітерують `app.routes`, тобто ловлять КОЖЕН новий
маршрут, а не той список, що існував на момент написання.

Postgres не потрібен: жоден тест не доходить до хендлера, що читає БД
(`/mock/webhook` — єдиний виняток, там `db()` підмінений). DATABASE_URL нижче
навмисно веде в нікуди — `admin.auth.init_epoch()` це переживає (warning +
епоха 1), і саме так поводиться контейнер, що стартував раніше за postgres.
"""

from __future__ import annotations

import os
import re
import time
from urllib.parse import parse_qs, urlparse

# Env — ДО імпорту admin.app: auth.py читає його на імпорті і навмисно падає,
# якщо пароль не заданий (fail-fast замість відкритого дашборда).
PASSWORD = "пароль-команди-42"
os.environ["DASHBOARD_PASSWORD"] = PASSWORD
os.environ["SESSION_SECRET"] = "0123456789abcdef" * 4  # 64 символи
os.environ["SESSION_IDLE_MINUTES"] = "30"
os.environ["DATABASE_URL"] = "postgresql://rfp:none@127.0.0.1:1/rfp"
os.environ["MOCK_N8N"] = "true"
os.environ["N8N_WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ.setdefault("N8N_URL", "https://n8n.example.test")

import pytest  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from admin import app as admin_app  # noqa: E402
from admin import auth  # noqa: E402

SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}

# Легасі-маршрути, чиї форми ще не мають прихованого поля csrf: партіал
# `_csrf.html` з'являється разом із новим base.html (крок 2), а поля в формах —
# на кроці 3. Крок 3 зобов'язаний СПОРОЖНИТИ цей список і повісити
# `Depends(csrf_guard)` на роутер (розділ 1.9). Новий POST-маршрут сюди
# додати не можна — тест на це і розрахований.
CSRF_LEGACY = {
    "/sources/add",
    "/sources/{source_id}/toggle",
    "/keywords/add",
    "/keywords/{keyword_id}/toggle",
    "/settings/save",
    "/kb/forums/add",
    "/kb/forums/{forum_id}/toggle",
    "/items/{item_uid}/brief",
}
# Ці двоє не мають і не повинні мати CSRF-токен: /login видається до сесії
# (токен виводиться з cookie, якої ще нема), /mock/webhook — публічний
# машинний вхід із власним секретом у заголовку.
CSRF_EXEMPT = {"/login", "/mock/webhook"}


@pytest.fixture()
def client():
    auth.reset_throttle()
    with TestClient(admin_app.app) as c:
        yield c


def _login(client) -> None:
    response = client.post(
        "/login", data={"password": PASSWORD, "next": "/"}, follow_redirects=False
    )
    assert response.status_code == 303


def _reason(response) -> str:
    query = parse_qs(urlparse(response.headers["location"]).query)
    return query.get("reason", [""])[0]


def _routes(method: str) -> list[APIRoute]:
    return [
        r
        for r in admin_app.app.routes
        if isinstance(r, APIRoute) and method in r.methods
    ]


# ── 1. Fail-closed на всіх маршрутах ───────────────────────────────


def test_every_get_route_requires_a_session(client):
    """Перетворює «fail closed» із заяви на факт: без cookie кожен GET, крім
    явного списку PUBLIC_EXACT ∪ PUBLIC_PREFIX, іде на /login."""
    checked = []
    for route in _routes("GET"):
        if route.path in auth.PUBLIC_EXACT or route.path.startswith(auth.PUBLIC_PREFIX):
            continue
        url = re.sub(r"\{[^}]+\}", "1", route.path)
        response = client.get(url, follow_redirects=False)
        assert response.status_code == 303, f"{route.path} віддав {response.status_code}"
        assert response.headers["location"].startswith("/login"), route.path
        checked.append(route.path)

    # Захист від «тест зелений, бо нічого не перевірив».
    assert "/" in checked and "/settings" in checked
    assert len(checked) >= 8, checked


# ── 2. CSRF-покриття мутуючих маршрутів ────────────────────────────


def test_every_post_route_is_csrf_covered():
    """Падає на новому незахищеному POST: щоб тест позеленів, автор має або
    повісити Depends(csrf_guard), або свідомо розширити список вище."""
    unprotected = []
    for route in _routes("POST"):
        if route.path in CSRF_EXEMPT or route.path in CSRF_LEGACY:
            continue
        deps = [d.call for d in route.dependant.dependencies]
        if auth.csrf_guard not in deps:
            unprotected.append(route.path)
    assert not unprotected, f"POST без csrf_guard: {unprotected}"

    # Легасі-список не має пережити крок 3, але поки він є — усі його шляхи
    # мусять існувати, інакше він тихо перетворюється на дірку для друкарської
    # помилки в новому маршруті.
    existing = {r.path for r in _routes("POST")}
    assert CSRF_LEGACY <= existing, CSRF_LEGACY - existing


# ── 3. Пароль ──────────────────────────────────────────────────────


def test_login_accepts_the_password_and_rejects_a_wrong_one(client):
    response = client.post(
        "/login",
        data={"password": PASSWORD, "next": "/items?status=pending"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/items?status=pending"
    assert auth.COOKIE_BASE in response.cookies  # http → без __Host-
    # Видана cookie справді відкриває застосунок.
    assert client.get("/session/ping", follow_redirects=False).status_code == 204

    auth.reset_throttle()
    client.cookies.clear()
    response = client.post(
        "/login", data={"password": PASSWORD + "x"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
    assert _reason(response) == "bad"


# ── 4. Ідл-таймаут ─────────────────────────────────────────────────


def test_session_older_than_the_idle_window_is_expired(client):
    class _StaleSigner(TimestampSigner):
        def get_timestamp(self) -> int:  # noqa: D401
            return int(time.time()) - auth.IDLE_SECONDS - 60

    stale = _StaleSigner(os.environ["SESSION_SECRET"], salt="rfp-admin-session-v1")
    client.cookies.set(
        auth.COOKIE_BASE, stale.sign(str(auth.current_epoch()).encode()).decode()
    )

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert _reason(response) == "expired"


# ── 5. Виняток для мока n8n ────────────────────────────────────────


def test_mock_webhook_works_without_a_session(client, monkeypatch):
    """Без цього винятку воркер у MOCK_N8N упирався б у редірект на /login, а
    симптом виплив би аж через PENDING_RETRY_AFTER_MINUTES."""

    class _Cursor:
        def fetchone(self):
            return None

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *args, **kwargs):
            return _Cursor()

        def commit(self):
            pass

    monkeypatch.setattr(admin_app, "db", _Conn)

    assert not client.cookies
    response = client.post(
        "/mock/webhook",
        json={"item_uid": "test-uid", "title": "t"},
        headers={"X-Webhook-Secret": os.environ["N8N_WEBHOOK_SECRET"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True


# ── 6. CSRF рівня транспорту ───────────────────────────────────────


def test_cross_site_post_is_rejected_before_the_handler(client):
    _login(client)
    response = client.post(
        "/settings/save",
        data={"confidence_threshold": "0.1"},
        headers={"Sec-Fetch-Site": "cross-site"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
    assert _reason(response) == "csrf"


# ── 7. Open redirect ───────────────────────────────────────────────


def test_safe_next_rejects_backslash_and_absolute_targets():
    # Chrome і Firefox нормалізують `\` у `/`, тож startswith("//") не ловить це.
    assert auth.safe_next("/\\evil.com") == "/"
    assert auth.safe_next("//evil.com") == "/"
    assert auth.safe_next("https://evil.com") == "/"
    assert auth.safe_next("/items?status=done\r\nSet-Cookie: x=1") == "/"
    assert auth.safe_next("/items?status=done") == "/items?status=done"
