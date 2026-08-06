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
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlparse

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

# Крок 3 завершено — список СПОРОЖНЕНО, як і вимагала специфікація: усі
# мутуючі маршрути тепер на роутері `mutations` з `Depends(csrf_guard)`, а
# форми інклюдять `_csrf.html`. Порожній набір означає, що будь-який новий
# POST без захисту завалить тест нижче.
CSRF_LEGACY: set[str] = set()
# Ці двоє не мають і не повинні мати CSRF-токен: /login видається до сесії
# (токен виводиться з cookie, якої ще нема), /mock/webhook — публічний
# машинний вхід із власним секретом у заголовку.
CSRF_EXEMPT = {"/login", "/mock/webhook"}


@pytest.fixture()
def client():
    auth.reset_throttle()
    with TestClient(admin_app.app) as c:
        yield c


WHO = auth.TEAM[0]  # будь-хто зі списку команди; підпис обов'язковий


def _login(client) -> None:
    response = client.post(
        "/login",
        data={"password": PASSWORD, "next": "/", "who": WHO},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _reason(response) == "", "логін мав пройти, а не повернутись на форму"


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

    # Захист від «тест зелений, бо перевіряти не було чого»: якби роутер
    # мутацій знову забули підключити (це вже траплялося — форми віддавали
    # 404), список POST-маршрутів схлопнувся б до /login і /mock/webhook.
    existing = {r.path for r in _routes("POST")}
    assert {"/sources/add", "/settings/save", "/logout"} <= existing, existing


# ── 3. Пароль ──────────────────────────────────────────────────────


def test_login_accepts_the_password_and_rejects_a_wrong_one(client):
    response = client.post(
        "/login",
        data={"password": PASSWORD, "next": "/items?status=pending", "who": WHO},
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
        "/login", data={"password": PASSWORD + "x", "who": WHO}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
    assert _reason(response) == "bad"


def test_signature_is_required_and_must_come_from_the_team_list(client):
    """`required` у HTML обходиться голим POST, тож перевірка — на сервері.

    Друга половина тесту важливіша за першу: якби довільний рядок приймався,
    вибір зі списку був би косметикою, і та сама людина знову підписувалася б
    по-різному.
    """
    for payload in ({"password": PASSWORD}, {"password": PASSWORD, "who": "хтось"}):
        auth.reset_throttle()
        client.cookies.clear()
        response = client.post("/login", data=payload, follow_redirects=False)
        assert response.status_code == 303
        assert _reason(response) == "who", payload
        assert auth.COOKIE_BASE not in response.cookies

    # А підпис зі списку доїжджає до колонок added_by/updated_by.
    auth.reset_throttle()
    client.cookies.clear()
    _login(client)
    request = SimpleNamespace(cookies={auth.COOKIE_BASE: client.cookies[auth.COOKIE_BASE]})
    assert auth.actor(request) == f"admin-ui:{WHO}"


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


# ── 8. Порожній env не має спустошувати список команди ─────────────


def _cookie_request(client) -> SimpleNamespace:
    """Мінімальний Request-подібний об'єкт для auth.session_who/session_sid:
    обом потрібен лише `.cookies`, а не повний Starlette Request (той самий
    прийом, що й у test_signature_is_required_and_must_come_from_the_team_list
    вище для auth.actor)."""
    return SimpleNamespace(cookies={auth.COOKIE_BASE: client.cookies[auth.COOKIE_BASE]})


def test_sid_is_minted_on_login_and_survives_a_resign(client):
    """Payload росте з "epoch|who" до "epoch|who|sid" (розділ 1 задачі чату):
    sid з'являється одразу на логіні і не губиться на rolling re-sign —
    інакше кожен запит під час чату відкривав би нову «розмову»."""
    _login(client)
    sid = auth.session_sid(_cookie_request(client))
    assert sid, "sid мав з'явитися одразу на логіні"
    assert auth.session_who(_cookie_request(client)) == WHO

    # Будь-який звичайний авторизований GET проходить через rolling re-sign
    # у middleware (auth.issue(response, request) без rotate_sid) — sid має
    # лишитися тим самим.
    response = client.get("/session/ping")
    assert response.status_code == 204
    assert auth.session_sid(_cookie_request(client)) == sid


def test_chat_new_rotates_sid_but_keeps_who(client):
    """POST /chat/new — auth.issue(..., rotate_sid=True): новий sid, той
    самий who/epoch. `/chat/new` навмисно в auth.NO_REISSUE (див. коментар
    там) — без цього rolling re-sign одразу після хендлера переписав би
    ротацію старим sid із request.cookies."""
    _login(client)
    before = auth.session_sid(_cookie_request(client))

    csrf = auth.csrf_for(client.cookies[auth.COOKIE_BASE])
    response = client.post(
        "/chat/new",
        data={"csrf": csrf},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/chat"

    after = auth.session_sid(_cookie_request(client))
    assert after and after != before, "ротація мала видати НОВИЙ sid"
    assert auth.session_who(_cookie_request(client)) == WHO


def test_legacy_two_part_cookie_still_authenticates_with_empty_sid(client, monkeypatch):
    """Cookie, видана до появи sid (payload "epoch|who", без третьої
    частини) — та сама, що й видавав старий auth.issue() до цієї зміни.
    Підпис лишається валідним (SESSION_SECRET не змінювався), тож повний
    редеплой не має розлогінити команду; /chat лише не бачить sid (fallback
    на legacy-{who}, admin/app.py)."""
    legacy_payload = f"{auth.current_epoch()}|{quote(WHO, safe='')}"
    client.cookies.set(
        auth.COOKIE_BASE, auth.signer.sign(legacy_payload.encode()).decode()
    )

    live, had_cookie = auth.session_live(_cookie_request(client))
    assert (live, had_cookie) == (True, True)
    assert auth.session_who(_cookie_request(client)) == WHO
    assert auth.session_sid(_cookie_request(client)) == ""

    # І справжній запит через застосунок теж проходить (не 303 на /login) —
    # db() підмінений, бо тут перевіряється лише сесія, не SQL /chat.
    class _Cursor:
        def fetchall(self):
            return []

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *args, **kwargs):
            return _Cursor()

    monkeypatch.setattr(admin_app, "db", _Conn)
    response = client.get("/chat", follow_redirects=False)
    assert response.status_code == 200


def test_empty_dashboard_users_env_falls_back_to_defaults(monkeypatch):
    """compose передає `DASHBOARD_USERS=${DASHBOARD_USERS:-}` — тобто змінна
    ІСНУЄ і порожня. `os.environ.get(key, default)` повернув би "", TEAM став би
    пустим і логін відхиляв би всіх. Перевірено наживо; тест фіксує поведінку."""
    import importlib

    monkeypatch.setenv("DASHBOARD_USERS", "")
    reloaded = importlib.reload(auth)
    try:
        assert reloaded.TEAM, "порожній env не має давати порожній список команди"
        assert reloaded.clean_who(reloaded.TEAM[0]) == reloaded.TEAM[0]
    finally:
        monkeypatch.undo()
        importlib.reload(auth)


def test_csrf_token_survives_rolling_resign(monkeypatch):
    """Регресія 2026-08-06 (знайдено наживо): csrf_for хешував ПОВНИЙ рядок
    cookie, а rolling re-sign оновлює timestamp у ньому кожною відповіддю —
    токен зі сторінки вмирав через секунду, і будь-яка форма, яку людина
    заповнює довше, летіла на /login?reason=csrf. TestClient цього не бачив,
    бо всі його запити вкладаються в одну секунду. Тому тут секунда
    перетинається ШТУЧНО: той самий payload підписується двічі з різницею
    в 5 «секунд» — токени мусять збігтися."""
    import time as _time

    payload = b"1|Growth|4244c388ffecf33435b8eb2435421e57"
    real_time = _time.time
    # «Старіший» підпис — у минулому: itsdangerous 2.2 відхиляє timestamp
    # з майбутнього (SignatureExpired), тож межу секунди переходимо назад.
    monkeypatch.setattr(_time, "time", lambda: real_time() - 5)
    cookie_t0 = auth.signer.sign(payload).decode()
    monkeypatch.undo()
    cookie_t5 = auth.signer.sign(payload).decode()

    assert cookie_t0 != cookie_t5, "re-sign мусить давати інший рядок cookie"
    token_t0 = auth.csrf_for(cookie_t0)
    token_t5 = auth.csrf_for(cookie_t5)
    assert token_t0 and token_t0 == token_t5, (
        "токен має бути функцією payload, а не timestamp"
    )


def test_csrf_token_differs_per_sid_and_rejects_garbage():
    """Токен прив'язаний до конкретного логіна (sid у payload) і не
    обчислюється для непідписаних рядків."""
    a = auth.csrf_for(auth.signer.sign(b"1|Growth|" + b"a" * 32).decode())
    b = auth.csrf_for(auth.signer.sign(b"1|Growth|" + b"b" * 32).decode())
    assert a and b and a != b
    assert auth.csrf_for("not-a-signed-cookie") == ""
    assert auth.csrf_for("") == ""
