"""Сесійна авторизація дашборда (специфікація «дашборд v2», розділ 1).

Механізм: підписаний cookie (`itsdangerous.TimestampSigner`) + глобальна епоха
відкликання в `settings.session_epoch`.

Чому не таблиця сесій у Postgres: єдиний спосіб накрити всі маршрути (зокрема
ті, що з'являться завтра) — middleware. Middleware у FastAPI асинхронний, а
`db()` в `app.py` — синхронний psycopg; пошук сесії в БД на кожному запиті прямо
в event loop серіалізував би весь застосунок. Підпис перевіряється без БД, а
«завершити всі сесії» — одна змінна в пам'яті процесу + один рядок у `settings`.

ІНВАРІАНТ (один воркер). `_EPOCH` кешується в пам'яті процесу, тому uvicorn
запускається БЕЗ `--workers` (див. admin/Dockerfile). Якщо колись з'являться
воркери, `_EPOCH` треба перечитувати з БД з TTL ~30 с через `run_in_threadpool`,
інакше `POST /logout/all` оновить епоху лише в тому воркері, який обробив запит.

ІНВАРІАНТ (порядок middleware). `app.add_middleware` робить
`user_middleware.insert(0, ...)`, тобто доданий ОСТАННІМ стає ЗОВНІШНІМ. Наш
guard зареєстрований першим у `app.py` і має лишатися найзовнішнім. Не додавати
перед ним `SessionMiddleware` (ми його не використовуємо) — інакше виняток у
ньому дасть 500 на всьому застосунку, повз guard.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from urllib.parse import urlencode, urlparse

import psycopg
from fastapi import Form, Request
from fastapi.responses import RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

log = logging.getLogger("admin.auth")

# ── Конфігурація ───────────────────────────────────────────────────

COOKIE_BASE = "rfp_session"
COOKIE_HOST = "__Host-rfp_session"  # використовується лише під https
EXP_COOKIE = "rfp_exp"  # НЕ HttpOnly, лише для таймера в UI

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
IDLE_SECONDS = int(os.environ.get("SESSION_IDLE_MINUTES", "30")) * 60

if not DASHBOARD_PASSWORD:
    raise RuntimeError(
        "DASHBOARD_PASSWORD не заданий — дашборд відмовляється стартувати відкритим"
    )
if len(SESSION_SECRET) < 32:
    raise RuntimeError(
        "SESSION_SECRET порожній або закороткий — потрібно щонайменше 32 символи "
        "(openssl rand -hex 32)"
    )

signer = TimestampSigner(SESSION_SECRET, salt="rfp-admin-session-v1")

# Дефолт для будь-якого невідомого шляху — вимагати сесію (fail closed).
# `/mock/webhook` — точним збігом, не префіксом `/mock/`: виняток стосується
# одного маршруту. `/logout` і `/logout/all` НЕ публічні — логаут
# неавторизованого не має сенсу і дає зайвий вектор.
PUBLIC_EXACT = {"/login", "/healthz", "/favicon.ico", "/mock/webhook"}
PUBLIC_PREFIX = ("/assets/",)

# Rolling re-sign не має воскрешати cookie, яку щойно видалив хендлер логауту:
# middleware виставляє Set-Cookie ПІСЛЯ хендлера, і браузер застосував би
# останній заголовок. Тому для цих шляхів cookie не перевидається.
NO_REISSUE = {"/logout", "/logout/all"}

# Причини на сторінці логіну (`?reason=`).
REASONS = {
    "expired": "Сесію завершено через 30 хвилин без активності",
    "bad": "Невірний пароль",
    "csrf": "Запит прийшов із чужої сторінки — увійди ще раз",
    "bye": "Ви вийшли",
    "throttled": "Забагато невдалих спроб — зачекай кілька секунд",
}
if IDLE_SECONDS != 1800:
    REASONS["expired"] = (
        f"Сесію завершено через {IDLE_SECONDS // 60} хв без активності"
    )


# ── Епоха відкликання ──────────────────────────────────────────────

_EPOCH = 1


def _read_epoch() -> int:
    """Ідемпотентний INSERT + SELECT `settings.session_epoch`.

    Це НЕ міграція, а один рядок даних: admin навмисно не викликає
    `worker.db.apply_migrations()` — вона не бере advisory lock, а
    `worker/main.py` кличе її на старті loop, тож при `up -d` обидва контейнери
    стартували б одночасно і один падав би в crash-loop.

    Стійкість до недоступної БД навмисна: застосунок без БД однаково марний
    (кожна сторінка все одно впаде), але процес має піднятися і віддавати
    `/healthz`, а тести не повинні вимагати Postgres.
    """
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        log.warning("DATABASE_URL не заданий — session_epoch=1 (відкликання сесій "
                    "не працюватиме до рестарту з робочою БД)")
        return 1
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_by) "
                "VALUES ('session_epoch', '1', 'admin-auth') "
                "ON CONFLICT (key) DO NOTHING"
            )
            row = conn.execute(
                "SELECT value FROM settings WHERE key = 'session_epoch'"
            ).fetchone()
            conn.commit()
            return int(row[0])
    except Exception as exc:  # noqa: BLE001 — старт не має залежати від БД
        log.warning(
            "session_epoch: БД недоступна (%s) — стартуємо з епохою 1; "
            "«Завершити всі сесії» полагодиться, щойно БД відповість", exc
        )
        return 1


def init_epoch() -> int:
    global _EPOCH
    _EPOCH = _read_epoch()
    return _EPOCH


def current_epoch() -> int:
    return _EPOCH


def bump_epoch() -> int:
    """`POST /logout/all`: інкремент епохи → всі видані cookie миттєво мертві."""
    global _EPOCH
    dsn = os.environ.get("DATABASE_URL", "")
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            row = conn.execute(
                "UPDATE settings SET value = (value::bigint + 1)::text, "
                "updated_at = now(), updated_by = 'admin-ui' "
                "WHERE key = 'session_epoch' RETURNING value"
            ).fetchone()
            conn.commit()
        if row:
            _EPOCH = int(row[0])
            return _EPOCH
    except Exception as exc:  # noqa: BLE001
        log.warning("logout/all: не вдалося оновити session_epoch у БД (%s) — "
                    "інкрементую лише в пам'яті процесу", exc)
    _EPOCH += 1
    return _EPOCH


init_epoch()


# ── Cookie ─────────────────────────────────────────────────────────


def _secure(request: Request) -> bool:
    """Обчислюється по-запитно, не з env: одна env-змінна не може бути
    водночас `true` для домену і `false` для аварійного SSH-тунелю на
    127.0.0.1:8080. Вимагає `--proxy-headers` у uvicorn, інакше за Caddy схема
    завжди `http` і cookie ніколи не стане Secure."""
    return request.url.scheme == "https"


def read_cookie(request: Request) -> str:
    """`__Host-` має пріоритет; читаються обидва імені."""
    return request.cookies.get(COOKIE_HOST) or request.cookies.get(COOKIE_BASE) or ""


def issue(response: Response, request: Request) -> None:
    """Rolling re-sign: свіжий timestamp на кожній успішній відповіді.
    Вікно ідлу рахується від останнього ЗАПИТУ, не від входу."""
    secure = _secure(request)
    name = COOKIE_HOST if secure else COOKIE_BASE
    response.set_cookie(
        name,
        signer.sign(str(_EPOCH).encode()).decode(),
        max_age=IDLE_SECONDS,
        httponly=True,
        secure=secure,
        samesite="lax",  # strict → перехід із Telegram приходить без cookie
        path="/",  # вимога __Host-; domain навмисно НЕ задається,
        # інакше cookie потече на n8n.{DOMAIN}
    )
    # Джерело правди для клієнтського таймера (app.js). Ні мишка, ні mousemove
    # таймер не скидають — інакше «неактивність» перестає щось означати.
    response.set_cookie(
        EXP_COOKIE,
        str(int(time.time()) + IDLE_SECONDS),
        max_age=IDLE_SECONDS,
        path="/",
        httponly=False,
        secure=secure,
        samesite="lax",
    )


def clear(response: Response, request: Request) -> None:
    secure = _secure(request)
    for name in (COOKIE_HOST, COOKIE_BASE):
        response.delete_cookie(name, path="/", httponly=True, secure=secure, samesite="lax")
    response.delete_cookie(EXP_COOKIE, path="/", samesite="lax", secure=secure)


def session_live(request: Request) -> tuple[bool, bool]:
    """→ (сесія жива, cookie взагалі була)."""
    raw = read_cookie(request)
    if not raw:
        return False, False
    try:
        payload = signer.unsign(raw.encode(), max_age=IDLE_SECONDS)
    except (BadSignature, SignatureExpired):
        return False, True
    return payload.decode() == str(_EPOCH), True


# ── Редірект на логін ──────────────────────────────────────────────


def safe_next(raw: str) -> str:
    """Перевірки `startswith("/") and not startswith("//")` НЕДОСТАТНЬО:
    Chrome і Firefox нормалізують `\\` у `/`, тож `/\\evil.com` дає
    protocol-relative URL."""
    if not raw or "\\" in raw or "\r" in raw or "\n" in raw:
        return "/"
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc or not raw.startswith("/"):
        return "/"
    return raw


def login_redirect(request: Request, reason: str = "") -> RedirectResponse:
    params: dict[str, str] = {}
    if reason:
        params["reason"] = reason
    if request.method in ("GET", "HEAD"):
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        target = safe_next(target)
        if target != "/":
            params["next"] = target
    url = "/login" + (("?" + urlencode(params)) if params else "")
    return RedirectResponse(url, status_code=303)


# ── Пароль і троттл брутфорсу ──────────────────────────────────────
#
# Один глобальний лічильник, БЕЗ IP. Per-IP бакет обходиться одним заголовком:
# Caddy v2 `reverse_proxy` ДОДАЄ IP клієнта до наявного X-Forwarded-For, а не
# замінює його, тож `[0]` підконтрольний атакувальнику; плюс порт 8080
# опублікований на loopback, де XFF довільний; плюс per-IP дозволяє отруїти
# чужий бакет. Пароль один на всіх → секрет для вгадування теж один.
# Лічильник у пам'яті процесу: при рестарті обнуляється, і це прийнятно — він
# не єдиний бар'єр (довгий пароль, HTTPS, HSTS).

_fails = 0
_blocked_until = 0.0


def reset_throttle() -> None:
    global _fails, _blocked_until
    _fails = 0
    _blocked_until = 0.0


async def verify_password(password: str) -> tuple[bool, int]:
    """→ (пароль вірний, скільки секунд ще блоковано)."""
    global _fails, _blocked_until
    now = time.monotonic()
    if now < _blocked_until:
        return False, int(_blocked_until - now) + 1

    # .encode() обов'язковий: hmac.compare_digest на str кидає TypeError на
    # неASCII-символах, а пароль генерує людина і UI український.
    ok = hmac.compare_digest(
        password.encode("utf-8"), DASHBOARD_PASSWORD.encode("utf-8")
    )
    if ok:
        reset_throttle()
        return True, 0

    _fails += 1
    _blocked_until = now + min(2 ** min(_fails, 9), 300)
    await asyncio.sleep(0.25)  # фіксована затримка на кожній невдачі
    return False, 0


# ── CSRF-токен у формах (третій шар) ───────────────────────────────
#
# Основний, fail-closed захист — перевірка Sec-Fetch-Site/Origin у middleware
# (app.py): вона покриває кожен маршрут, включно з тими, яких ще нема. Токен
# нижче — третій шар і вішається per-route, тож його відсутність на новому
# маршруті вже не є діркою.


class CsrfError(Exception):
    """Обробляється @app.exception_handler → редірект на /login?reason=csrf."""


def csrf_for(raw_cookie: str) -> str:
    """Виводиться детерміновано з cookie, тому не потребує сховища."""
    if not raw_cookie:
        return ""
    return hmac.new(
        SESSION_SECRET.encode(), raw_cookie.encode(), "sha256"
    ).hexdigest()[:32]


async def csrf_guard(request: Request, csrf: str = Form("")) -> None:
    """`csrf: str = Form("")`, а не `await request.form()`: другий варіант
    спирається на приватний `Request._form`, і мовчазна регресія виглядатиме
    як «форма зберігає порожні значення»."""
    raw = read_cookie(request)
    if not raw or not hmac.compare_digest(csrf, csrf_for(raw)):
        raise CsrfError()


def template_context(request: Request) -> dict:
    """Context processor для Jinja2Templates — `{% include "_csrf.html" %}`
    бачить `csrf` на будь-якій сторінці, без правок кожного хендлера."""
    return {"csrf": csrf_for(read_cookie(request))}
