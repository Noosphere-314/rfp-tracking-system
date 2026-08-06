"""Панель команди — дашборд RFP Tracker.

Доступ (F16, змінено комітом c5d3e68). Раніше сервіс жив за Tailscale і
твердження «never exposed through Caddy» було правдою; тепер дашборд стоїть на
публічному домені, а його межа безпеки — сесійний логін у самому застосунку
(`admin/auth.py`): підписаний cookie, ідл-таймаут, CSRF fail-closed. Порт
127.0.0.1:8080 лишається аварійним входом через SSH-тунель, коли Caddy або
домен зламані, і той самий логін діє й там.

Панель для людей, що ведуть систему: здоров'я, керування джерелами з живим
тест-фетчем, редагування ключових слів і порогів, перегляд знахідок і прогонів.
Не-інженери користуються n8n-формами на /form/* (там лишається basic_auth).

Тут же живе локальний мок пайплайна n8n (MOCK_N8N=true): воркер може навести
N8N_WEBHOOK_URL сюди і повний цикл deliver→confirm проходить наскрізь без
Pipedrive, Claude і n8n. Публічно цей маршрут закритий на рівні Caddy
(`handle /mock/* { respond 404 }`).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

import psycopg
import regex
from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from psycopg.rows import dict_row

from admin import auth, i18n
from worker import fetchers
from worker.fetchers.base import Source
from worker.http import HttpClient, SourceBlocked

log = logging.getLogger("admin")

DATABASE_URL = os.environ["DATABASE_URL"]
MOCK_N8N = os.environ.get("MOCK_N8N", "").lower() in ("1", "true", "yes")
WEBHOOK_SECRET = os.environ.get("N8N_WEBHOOK_SECRET", "")

STATIC = Path(__file__).parent / "static"

# Кеш-бастинг: один stat на імпорті → ?v= у base.html і login.html. Без нього
# правка CSS не доїжджає до браузерів, які тримають старий файл у кеші.
try:
    ASSET_V = int((STATIC / "app.css").stat().st_mtime)
except OSError:  # ассетів немає — краще 0, ніж 500 на кожній сторінці
    ASSET_V = 0

# Пункт меню «n8n» — з env, не хардкод: локально DOMAIN=localhost, і зашитий
# прод-URL вів би дев-режим на прод.
N8N_URL = os.environ.get("N8N_URL", "")

# openapi_url=None, бо docs_url=None НЕ вимикає /openapi.json: карта всіх
# маршрутів на публічному домені не має цінності для команди і має ненульову —
# для сканера.
app = FastAPI(
    title="RFP Tracker Admin", docs_url=None, redoc_url=None, openapi_url=None
)
templates = Jinja2Templates(
    directory=Path(__file__).parent / "templates",
    context_processors=[auth.template_context, i18n.template_context],
)
# /assets/, а не /static/: каддівський catch-all віддає адміну все, що не
# /webhook/*, /form/*, /form-waiting-room/*. Якщо HTML n8n-форми колись
# запросить щось із /static/..., запит прийде в адмінку і форма зламається.
app.mount("/assets", StaticFiles(directory=STATIC), name="assets")


def db() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, client_encoding="utf8")


# ── Презентація: навігація, словники, фільтри (розділ 4) ───────────
#
# Навігація живе тут, а не в шаблоні (розділ 2.3): активний пункт визначається
# nav-ідентифікатором із контексту маршруту, а не
# `request.url.path.startswith()` — останній зробив би «/» активним завжди.

NAV_GROUPS = [("work", "Робота"), ("cfg", "Налаштування"), ("sys", "Система")]
NAV = [
    {"id": "dashboard", "href": "/", "label": "Огляд", "group": "work"},
    {"id": "items", "href": "/items", "label": "Знахідки", "group": "work"},
    {"id": "kb", "href": "/kb", "label": "База знань", "group": "work"},
    {"id": "chat", "href": "/chat", "label": "AI-чат", "group": "work"},
    {"id": "sources", "href": "/sources", "label": "Джерела", "group": "cfg"},
    {"id": "keywords", "href": "/keywords", "label": "Ключові слова", "group": "cfg"},
    {"id": "settings", "href": "/settings", "label": "Параметри", "group": "cfg"},
    {"id": "runs", "href": "/runs", "label": "Історія збору", "group": "sys"},
    {"id": "briefs", "href": "/briefs", "label": "Бріфи", "group": "sys"},
]

# ІНВАРІАНТ ЛОКАЛІЗАЦІЇ: українська ТІЛЬКИ в презентації. Значення в URL і в БД
# лишаються англійськими (`?status=done`) — інакше ламаються закладки,
# посилання з Telegram і фільтри, які вміє воркер. Скрізь `.get(x, x)`:
# у БД може з'явитися нове значення (F13 — це один рядок міграції), і воно має
# показатися як є, а не зникнути.
ITEM_STATUSES = ["pending", "done", "dead", "seeded", "filtered"]
STATUS_UA = {
    "pending": "у черзі",
    "done": "оброблено",
    "dead": "не доставлено",
    "seeded": "засіяно",
    "filtered": "відсіяно",
}
LANE_UA = {"rfp": "RFP", "funding": "фандинг"}
KIND_UA = {"include": "пропускає", "exclude": "відсікає"}
MODE_UA = {"run": "звичайний", "seed": "засів"}
TIER_UA = {"llm": "LLM", "basic": "базовий"}


def hl(value: str) -> Markup:
    """Підсвітка сніпета `ts_headline` (розділ 4.7).

    Порядок «екранувати → підставити теги» тут і є вся безпека: `raw_text`
    форумного поста може містити літеральний `<script>`, тож спершу все
    екранується, і лише потім керуючі сентинели \\x02/\\x03 (їх не буває
    в тексті) стають `<mark>`. `|safe` до сніпета не додавати НІ ЗА ЯКИХ УМОВ.
    Бонус: перестають ламатися легітимні лапки «» в українських постах.
    """
    text = str(escape(value or ""))
    return Markup(text.replace("\x02", "<mark>").replace("\x03", "</mark>"))


# Стандартна `re`, а не сторонній `regex` вище (той — під keyword-патерни
# користувачів, тут — фіксований власний вираз без потреби в його фічах).
_URL_RE = re.compile(r'https?://[^\s<>"\']+')


def linkify(value: str) -> Markup:
    """Відповідь AI-чату (розділ 4.9): markdown НЕ рендериться (те саме
    рішення, що й для brief.html — власний рендерер [текст](url) був би
    XSS-поверхнею), але голий URL у відповіді моделі має бути клікабельним.

    Порядок «екранувати → підставити теги» — той самий принцип, що й у hl():
    спершу ВЕСЬ текст втрачає html-сенс, і лише потім підрядки, що після
    escape() лишились незмінними (URL не містить символів, які escape()
    чіпає), обгортаються в <a>. Застосовується лише до тіла асистента —
    повідомлення людини рендериться голим текстом нижче в шаблоні.
    """
    text = str(escape(value or ""))
    return Markup(
        _URL_RE.sub(
            lambda m: f'<a href="{m.group(0)}" target="_blank" rel="noopener">{m.group(0)}</a>',
            text,
        )
    )


templates.env.filters["hl"] = hl
templates.env.filters["linkify"] = linkify
# Константи презентації — глобалами Jinja, а не context-процесором: вони
# однакові для кожного запиту, а context-процесори Starlette перезаписують
# контекст сторінки (`context.update(...)` після нього), тож усе, що маршрут
# може перевизначити (`nav`, `message`, `error`), сюди класти НЕ можна.
templates.env.globals.update(
    NAV=NAV,
    NAV_GROUPS=NAV_GROUPS,
    N8N_URL=N8N_URL,
    asset_v=ASSET_V,
    mock_n8n=MOCK_N8N,
    ITEM_STATUSES=ITEM_STATUSES,
    STATUS_UA=STATUS_UA,
    LANE_UA=LANE_UA,
    KIND_UA=KIND_UA,
    MODE_UA=MODE_UA,
    TIER_UA=TIER_UA,
)

# Усі мутуючі маршрути — на одному роутері з `csrf_guard` (розділ 1.9, третій
# шар). Роутер, а не декоратор поштучно: новий POST потрапляє під захист самим
# фактом реєстрації тут. Основний, fail-closed бар'єр — перевірка
# Sec-Fetch-Site/Origin у middleware вище; `/mock/webhook` навмисно лишається
# поза цим роутером (машинний вхід із власним секретом у заголовку).
mutations = APIRouter(dependencies=[Depends(auth.csrf_guard)])


# ── Авторизація ────────────────────────────────────────────────────
#
# Guard — рівно один middleware, зареєстрований першим у файлі, тобто
# найзовнішній (add_middleware вставляє на позицію 0). Порядок операцій у тілі
# обов'язковий, див. розділ 1.7 специфікації.


@app.middleware("http")
async def session_guard(request: Request, call_next):
    path = request.url.path

    # 1. Виключення — ПЕРШИМИ. /mock/webhook не бачить ні auth, ні CSRF.
    if path in auth.PUBLIC_EXACT or path.startswith(auth.PUBLIC_PREFIX):
        return await call_next(request)

    # 2. CSRF рівня транспорту — fail CLOSED, без читання тіла (читання body в
    #    BaseHTTPMiddleware робить форму порожньою в ендпоінті). Покриває кожен
    #    маршрут, включно з тими, яких ще нема; SameSite=Lax тут другий шар, бо
    #    rolling re-sign тримає cookie постійно всередині вікна Chrome
    #    «Lax+POST», де top-level cross-site POST cookie ще несе.
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        site = request.headers.get("sec-fetch-site")
        origin = request.headers.get("origin")
        host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
        same = (site == "same-origin") or bool(
            origin and urlparse(origin).netloc == host
        )
        if not same:
            return auth.login_redirect(request, "csrf")

    # 3. Сесія
    live, had_cookie = auth.session_live(request)
    if not live:
        return auth.login_redirect(request, "expired" if had_cookie else "")

    response = await call_next(request)

    # 4. Rolling re-sign + companion cookie + no-store
    if path not in auth.NO_REISSUE:
        auth.issue(response, request)
    if "text/html" in response.headers.get("content-type", ""):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


@app.exception_handler(auth.CsrfError)
async def csrf_error_handler(request: Request, exc: auth.CsrfError):
    # HTML-редірект, а не голий JSON {"detail": …}.
    return RedirectResponse("/login?reason=csrf", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, reason: str = "", next: str = "/", who: str = ""):
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "reason": reason,
            "reason_text": auth.REASONS.get(reason, ""),
            "next": auth.safe_next(next),
            "idle_minutes": auth.IDLE_SECONDS // 60,
            "team": auth.TEAM,
            "who": who,
        },
    )


@app.post("/login")
async def login_submit(
    request: Request,
    password: str = Form(...),
    next: str = Form("/"),
    who: str = Form(""),
):
    """`who` — обов'язковий вибір зі списку команди (варіант C з розділу 7, п.5).

    Це журнал змін, а не автентифікація: пароль спільний, тож система не може
    знати, хто саме увійшов. Але коли хтось змінює поріг класифікатора,
    `settings.updated_by` має відповідати на «хто це зробив» — інакше колонка
    назавжди лишається 'admin-ui'.

    Пароль перевіряється ПЕРШИМ, ще до `who`: інакше форма підказувала б
    сторонньому, що пароль підійшов, і перетворювала б підбір на двокроковий.
    """
    target = auth.safe_next(next)
    ok, blocked_for = await auth.verify_password(password)
    if not ok:
        reason = "throttled" if blocked_for else "bad"
        log.warning("невдалий вхід у дашборд (reason=%s)", reason)
        params = {"reason": reason}
        if target != "/":
            params["next"] = target
        return RedirectResponse(f"/login?{urlencode(params)}", status_code=303)

    signature = auth.clean_who(who)
    if not signature:
        # `required` у HTML обходиться голим POST — справжня перевірка тут.
        params = {"reason": "who"}
        if target != "/":
            params["next"] = target
        return RedirectResponse(f"/login?{urlencode(params)}", status_code=303)

    log.info("вхід у дашборд: %s", signature)
    response = RedirectResponse(target, status_code=303)
    auth.issue(response, request, who=signature)
    return response


# /logout тільки POST: GET-логаут префетчиться браузерами і спрацьовує від
# чужого <img src>.
@app.post("/logout", dependencies=[Depends(auth.csrf_guard)])
def logout(request: Request):
    response = RedirectResponse("/login?reason=bye", status_code=303)
    auth.clear(response, request)
    return response


@app.post("/logout/all", dependencies=[Depends(auth.csrf_guard)])
def logout_all(request: Request):
    """Кнопка на /settings — природне місце після ротації пароля."""
    epoch = auth.bump_epoch()
    log.info("усі сесії завершено, session_epoch=%s", epoch)
    response = RedirectResponse("/login?reason=bye", status_code=303)
    auth.clear(response, request)
    return response


@app.post("/lang", dependencies=[Depends(auth.csrf_guard)])
def switch_lang(request: Request, lang: str = Form(...), next: str = Form("/")):
    """Перемикач мови в кабінеті. Дефолт — англійська; UA лише за явним вибором.

    Мова живе в окремій cookie, а не в сесії: це преференція перегляду, а не
    частина автентифікації, і перевидавати підписаний сесійний cookie заради
    косметики не варто.
    """
    response = RedirectResponse(auth.safe_next(next), status_code=303)
    response.set_cookie(
        i18n.LANG_COOKIE,
        i18n.normalize(lang),
        max_age=365 * 24 * 3600,
        path="/",
        httponly=False,
        secure=request.url.scheme == "https",
        samesite="lax",
    )
    return response


@app.get("/session/ping")
def session_ping():
    """204, no-op — кнопка «Залишитись» у банері таймера. Окремий keepalive не
    потрібен: middleware і так продовжив сесію ще до входу в хендлер."""
    return Response(status_code=204)


@app.get("/healthz")
def healthz():
    """Публічний. Compose healthcheck б'є сюди: без нього `restart:
    unless-stopped` + fail-fast на env дають нескінченний crash-loop, який
    `docker compose ps` показує як «running»."""
    try:
        with db() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:  # noqa: BLE001
        log.warning("healthz: БД не відповідає (%s)", exc)
        return JSONResponse({"ok": False, "db": False}, status_code=503)
    return {"ok": True}


# ── Dashboard ──────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with db() as conn:
        status_counts = {
            row["status"]: row["n"]
            for row in conn.execute(
                "SELECT status, count(*) AS n FROM seen_items GROUP BY status"
            )
        }
        last_runs = conn.execute(
            "SELECT * FROM worker_runs ORDER BY started_at DESC LIMIT 5"
        ).fetchall()
        # Сортування «найпроблемніші зверху» + LIMIT (розділ 4.1): попереднє
        # `ORDER BY enabled DESC, name` без ліміту ховало зламане джерело
        # посеред алфавіту. На головній має бути видно проблему, а не повний
        # реєстр — повний живе на /sources.
        source_health = conn.execute(
            """
            SELECT s.id, s.name, s.type, s.ecosystem, s.enabled, s.quarantined,
                   s.quarantine_reason, s.lane, s.last_success_at, s.last_item_at,
                   s.consecutive_failures,
                   count(i.item_uid) FILTER (WHERE i.first_seen > now() - interval '7 days') AS items_7d
              FROM sources s LEFT JOIN seen_items i ON i.source_id = s.id
             GROUP BY s.id
             ORDER BY s.consecutive_failures DESC, s.last_item_at ASC NULLS FIRST
             LIMIT 12
            """
        ).fetchall()
        sources_total = conn.execute("SELECT count(*) AS n FROM sources").fetchone()["n"]
        # Чесний бейдж режиму (розділ 4.1, F6): поки класифікатор — заглушка з
        # захардкодженим confidence 0.55, будь-яка аналітика по впевненості
        # малювала б сотню однакових барів і виглядала б робочою.
        last_verdict = conn.execute(
            "SELECT prompt_version FROM items_log WHERE event = 'classified' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        recent_pending = conn.execute(
            """
            SELECT i.*, s.name AS source_name FROM seen_items i
              JOIN sources s ON s.id = i.source_id
             WHERE i.status IN ('pending', 'dead')
             ORDER BY i.first_seen DESC LIMIT 20
            """
        ).fetchall()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "nav": "dashboard",
            "status_counts": status_counts,
            "last_runs": last_runs,
            "source_health": source_health,
            "sources_total": sources_total,
            "recent_pending": recent_pending,
            "stub_classifier": bool(
                last_verdict and last_verdict["prompt_version"] == "stub-no-llm"
            ),
        },
    )


# ── Sources ────────────────────────────────────────────────────────


SOURCE_FORM_FIELDS = ("type", "name", "ecosystem", "url", "category", "lane", "config")


def _render_sources(
    request: Request,
    *,
    message: str = "",
    error: str = "",
    form: dict | None = None,
    status_code: int = 200,
):
    """Одна точка рендеру /sources — щоб помилкова гілка `add_source` могла
    повернути сторінку з уже введеними значеннями (розділ 4.2), а не редірект,
    після якого 7 полів і JSON-конфіг доводиться набирати заново."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM sources ORDER BY enabled DESC, quarantined, type, name"
        ).fetchall()
    return templates.TemplateResponse(
        request,
        "sources.html",
        {
            "nav": "sources",
            "sources": rows,
            "fetcher_types": sorted(fetchers.FETCHERS),
            "message": message,
            "error": error,
            "form": {key: (form or {}).get(key, "") for key in SOURCE_FORM_FIELDS},
        },
        status_code=status_code,
    )


@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, message: str = "", error: str = ""):
    return _render_sources(request, message=message, error=error)


@mutations.post("/sources/{source_id}/toggle")
def toggle_source(source_id: int):
    with db() as conn:
        conn.execute(
            "UPDATE sources SET enabled = NOT enabled, quarantined = false, "
            "quarantine_reason = NULL WHERE id = %s",
            (source_id,),
        )
        conn.commit()
    return RedirectResponse("/sources", status_code=303)


def _test_fetch(row: dict) -> tuple[int, str]:
    """Run the real fetcher once, read-only. Returns (count, error)."""
    source = Source.from_row(row)
    fetch = fetchers.get(source.type)
    since = datetime.now(timezone.utc) - timedelta(days=30)
    with db() as conn, HttpClient(conn) as client:
        try:
            items = []
            for raw in fetch(source, client, since):
                items.append(raw)
                if len(items) >= 5:
                    break
            return len(items), ""
        except SourceBlocked as exc:
            return 0, f"blocked (403/429): {exc}"
        except Exception as exc:  # noqa: BLE001 — anything goes wrong = don't save
            return 0, f"{type(exc).__name__}: {exc}"


@mutations.post("/sources/add")
def add_source(
    request: Request,
    type: str = Form(...),
    name: str = Form(...),
    ecosystem: str = Form(...),
    url: str = Form(...),
    category: str = Form(""),
    lane: str = Form("rfp"),
    config: str = Form("{}"),
):
    """Додавання з живим тест-фетчем — та сама гарантія, що дає n8n-форма:
    джерело, яке зараз не може віддати жодного елемента, не зберігається
    увімкненим.

    Помилкові гілки повертають ВІДРЕНДЕРЕНУ сторінку зі збереженими полями
    (розділ 4.2), а не редірект: тест-фетч триває до 30 с, і втрачати після
    нього сім полів разом із JSON-конфігом — найдорожча дрібниця цієї сторінки.
    """
    submitted = {
        "type": type, "name": name, "ecosystem": ecosystem, "url": url,
        "category": category, "lane": lane, "config": config,
    }
    if type not in fetchers.FETCHERS:
        return _render_sources(
            request, error=f"Unknown source type: {type}",
            form=submitted, status_code=400,
        )
    try:
        config_obj = json.loads(config or "{}")
        if not isinstance(config_obj, dict):
            raise ValueError("конфіг має бути JSON-об'єктом {…}")
    except ValueError as exc:
        return _render_sources(
            request, error=f"Invalid JSON in config: {exc}",
            form=submitted, status_code=400,
        )

    candidate = {
        "id": 0, "type": type, "name": name.strip(), "ecosystem": ecosystem.strip(),
        "url": url.strip(), "category": category.strip() or None,
        "config": config_obj, "lane": lane,
    }
    count, error = _test_fetch(candidate)
    if error:
        return _render_sources(
            request, error=f"Test fetch failed: {error}",
            form=submitted, status_code=400,
        )

    with db() as conn:
        conn.execute(
            """
            INSERT INTO sources (type, name, ecosystem, url, category, config, lane,
                                 enabled, added_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, true, %s)
            ON CONFLICT (type, url, category) DO UPDATE
                SET name = EXCLUDED.name, config = EXCLUDED.config,
                    enabled = true, quarantined = false, quarantine_reason = NULL
            """,
            (type, candidate["name"], candidate["ecosystem"], candidate["url"],
             candidate["category"], json.dumps(config_obj), lane,
             auth.actor(request)),
        )
        conn.commit()
    return RedirectResponse(
        f"/sources?message=Saved — test fetch returned {count} item(s)",
        status_code=303,
    )


# ── Keywords ───────────────────────────────────────────────────────


@app.get("/keywords", response_class=HTMLResponse)
def keywords_page(request: Request, message: str = "", error: str = ""):
    with db() as conn:
        rows = conn.execute("SELECT * FROM keywords ORDER BY kind, id").fetchall()
    # Дві протилежні за змістом сутності розводяться по двох панелях (розділ
    # 4.4): в одній таблиці вони розрізнялися лише бейджем — постійне джерело
    # помилок прочитання. Невалідні закріплені зверху окремою секцією.
    return templates.TemplateResponse(
        request,
        "keywords.html",
        {
            "nav": "keywords",
            "invalid": [k for k in rows if not k["valid"]],
            "include": [k for k in rows if k["valid"] and k["kind"] == "include"],
            "exclude": [k for k in rows if k["valid"] and k["kind"] == "exclude"],
            "message": message,
            "error": error,
        },
    )


@mutations.post("/keywords/add")
def add_keyword(request: Request, pattern: str = Form(...), kind: str = Form(...)):
    if kind not in ("include", "exclude"):
        raise HTTPException(400, "kind must be include or exclude")
    # Compile-check at write time (A4); read-time quarantine still applies.
    try:
        regex.compile(pattern, regex.IGNORECASE)
    except regex.error as exc:
        return RedirectResponse(
            f"/keywords?error=Pattern does not compile: {exc}", status_code=303
        )

    with db() as conn:
        conn.execute(
            "INSERT INTO keywords (pattern, kind, added_by) VALUES (%s, %s, %s) "
            "ON CONFLICT (pattern, kind) DO UPDATE SET enabled = true",
            (pattern, kind, auth.actor(request)),
        )
        conn.commit()
    return RedirectResponse("/keywords?message=Saved", status_code=303)


@mutations.post("/keywords/{keyword_id}/toggle")
def toggle_keyword(keyword_id: int):
    with db() as conn:
        conn.execute(
            "UPDATE keywords SET enabled = NOT enabled WHERE id = %s", (keyword_id,)
        )
        conn.commit()
    return RedirectResponse("/keywords", status_code=303)


# ── Settings ───────────────────────────────────────────────────────
#
# `session_epoch` — службовий лічильник відкликання сесій (admin/auth.py).
# Не рендериться і не редагується: ручна правка розлогінила б команду без
# жодного натяку на причину.
HIDDEN_SETTINGS = {"session_epoch"}

# Англійська — дефолт (живе тут), українська — ключем у i18n.UK: той самий
# принцип, що і в шаблонах, EN видно при читанні коду.
SETTING_GROUPS = [
    ("classify", "Classification"),
    ("volume", "Volume and limits"),
    ("channels", "Channels"),
    ("leads", "Leads"),
    ("other", "Other"),
]

# Підказки навмисно чесні (розділ 7.12): кілька ключів зараз не читає ніхто,
# і підказка, що описує неіснуючу поведінку, гірша за її відсутність.
SETTING_META = {
    "confidence_threshold": {
        "group": "classify", "label": "Auto-lead threshold", "type": "float",
        "hint": "0…1. Above the threshold a lead is created automatically. Read by n8n (rfp-main).",
    },
    "review_band_low": {
        "group": "classify", "label": "Review band, lower bound", "type": "float",
        "hint": "0…1, not above the auto-lead threshold. Anything in between goes to the review channel.",
    },
    "classifier_prompt_version": {
        "group": "classify", "label": "Prompt version", "type": "text",
        "hint": "Not in use yet: in STUB mode the classifier writes prompt_version itself.",
    },
    "max_leads_per_run": {
        "group": "volume", "label": "Max leads per run", "type": "int",
        "min": 1, "max": 500,
        "hint": "1…500. Not in use yet — no component reads this key.",
    },
    "lead_floor_7d": {
        "group": "volume", "label": "Minimum leads per 7 days", "type": "int",
        "min": 0, "max": 100,
        "hint": "0…100. Fewer than that raises an alert (read by n8n rfp-digest).",
    },
    "source_dark_days": {
        "group": "volume", "label": "Days of source silence before an alert", "type": "int",
        "min": 1, "max": 365,
        "hint": "1…365. Read by the worker on every run — a non-numeric value stops the run mid-cycle.",
    },
    "alert_channel": {
        "group": "channels", "label": "Alerts channel", "type": "channel",
        "hint": "Starts with #. Not in use yet: Slack nodes are disabled, notifications go to Telegram.",
    },
    "review_channel": {
        "group": "channels", "label": "Review channel", "type": "channel",
        "hint": "Starts with #. Not in use yet (Slack nodes are disabled).",
    },
    "digest_channel": {
        "group": "channels", "label": "Digest channel", "type": "channel",
        "hint": "Starts with #. Not in use yet (Slack nodes are disabled).",
    },
    "lead_title_template": {
        "group": "leads", "label": "Lead title template", "type": "text",
        "hint": "Placeholders {label}, {ecosystem}, {title}. Not in use yet.",
    },
    "chat_model": {
        "group": "other", "label": "Chat model", "type": "text",
        "hint": "Anthropic model id for the KB chat; applies without redeploy.",
    },
}


def validate_setting(key: str, value: str) -> tuple[bool, str]:
    """→ (ок, текст помилки англійською: це системна діагностика).

    Без цієї перевірки `0.7.` у `confidence_threshold` тихо ламає класифікацію
    на наступному прогоні, а «14 днів» у `source_dark_days` кидає ValueError
    прямо в pipeline.py посеред циклу (F11). Редизайн робить сторінку зручною,
    тобто підвищує шанси, що сюди хтось надрукує.
    """
    meta = SETTING_META.get(key, {})
    kind = meta.get("type", "text")
    label = meta.get("label", key)
    value = value.strip()

    if kind == "float":
        try:
            number = float(value)
        except ValueError:
            return False, f"{label}: expected a number between 0 and 1, got “{value}”"
        if not 0.0 <= number <= 1.0:
            return False, f"{label}: the number must be within 0…1"
    elif kind == "int":
        try:
            number = int(value)
        except ValueError:
            return False, f"{label}: expected a whole number, got “{value}”"
        low, high = meta.get("min", 0), meta.get("max", 10**9)
        if not low <= number <= high:
            return False, f"{label}: the number must be within {low}…{high}"
    elif kind == "channel":
        if not value.startswith("#"):
            return False, f"{label}: the channel name must start with #"
    if not value:
        return False, f"{label}: empty value"
    return True, ""


def _to_float(value, default: float) -> float:
    try:
        return min(max(float(value), 0.0), 1.0)
    except (TypeError, ValueError):
        return default


def _render_settings(
    request: Request,
    *,
    message: str = "",
    error: str = "",
    overrides: dict | None = None,
    status_code: int = 200,
):
    with db() as conn:
        rows = conn.execute("SELECT * FROM settings ORDER BY key").fetchall()

    # Мітки й підказки перекладаються тут, а не в шаблоні: вони приходять із
    # SETTING_META (англійська там і живе), а українська — ключем у i18n.UK.
    tr = i18n.translator(i18n.lang_of(request))

    values, grouped = {}, {}
    for row in rows:
        if row["key"] in HIDDEN_SETTINGS:
            continue
        item = dict(row)
        if overrides and row["key"] in overrides:
            item["value"] = overrides[row["key"]]
        meta = SETTING_META.get(row["key"], {})
        # Ключ, якого нема в мапі, лишається видимим звичайним текстовим полем:
        # нова міграція не має робити налаштування невидимим.
        item["label"] = tr(f"set.{row['key']}.label", meta.get("label", row["key"]))
        item["hint"] = tr(f"set.{row['key']}.hint", meta.get("hint", "")) if meta.get("hint") else ""
        item["kind"] = meta.get("type", "text")
        values[row["key"]] = item["value"]
        grouped.setdefault(meta.get("group", "other"), []).append(item)

    low = _to_float(values.get("review_band_low"), 0.4)
    high = _to_float(values.get("confidence_threshold"), 0.7)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "nav": "settings",
            "groups": [
                (gid, tr(f"setg.{gid}", label), grouped[gid])
                for gid, label in SETTING_GROUPS
                if gid in grouped
            ],
            # Смуга подвійного порога (A8) малюється як SVG із порахованими
            # координатами: CSP забороняє і атрибут style, і <style>, а
            # SVG-атрибути дозволені.
            "band": {"low": low, "high": min(max(high, low), 1.0)},
            "message": message,
            "error": error,
        },
        status_code=status_code,
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, message: str = ""):
    return _render_settings(request, message=message)


@mutations.post("/settings/save")
async def save_settings(request: Request):
    form = await request.form()
    submitted = {k: v for k, v in form.items() if isinstance(v, str)}

    with db() as conn:
        # Whitelist із БД, а не з форми (F11): раніше сюди писався БУДЬ-ЯКИЙ
        # ключ форми, включно з `csrf` і будь-яким майбутнім службовим полем.
        allowed = {
            row["key"] for row in conn.execute("SELECT key FROM settings")
        } - HIDDEN_SETTINGS
        current = {
            row["key"]: row["value"]
            for row in conn.execute("SELECT key, value FROM settings")
        }

        pending = {}
        for key, value in submitted.items():
            if key not in allowed:
                continue
            ok, err = validate_setting(key, value)
            if not ok:
                return _render_settings(
                    request, error=err, overrides=submitted, status_code=400
                )
            pending[key] = value.strip()

        # Перехресна перевірка: 0 ≤ review_band_low ≤ confidence_threshold ≤ 1.
        merged = {**current, **pending}
        low, high = merged.get("review_band_low"), merged.get("confidence_threshold")
        if low is not None and high is not None:
            try:
                if float(low) > float(high):
                    return _render_settings(
                        request,
                        error="The review band lower bound cannot exceed the auto-lead "
                              "threshold — the band would be empty and some "
                              "findings would vanish silently",
                        overrides=submitted,
                        status_code=400,
                    )
            except ValueError:
                pass

        # `value IS DISTINCT FROM %s` навмисно: підпис і updated_at міняються
        # лише для ключів, які реально змінилися, тож «хто останній чіпав поріг»
        # не затирається тим, хто просто натиснув «Зберегти все».
        actor = auth.actor(request)
        for key, value in pending.items():
            conn.execute(
                "UPDATE settings SET value = %s, updated_at = now(), "
                "updated_by = %s WHERE key = %s AND value IS DISTINCT FROM %s",
                (value, actor, key, value),
            )
        conn.commit()
    return RedirectResponse("/settings?message=Saved", status_code=303)


# ── Items & runs ───────────────────────────────────────────────────


@app.get("/items", response_class=HTMLResponse)
def items_page(
    request: Request,
    status: str = "",
    source_id: int = 0,
    q: str = "",
    page: int = 0,
):
    # Дефолт «усі» НЕ змінюється (розділ 4.3): `done` не означає ліда (F5), а
    # зміна дефолту тихо перевизначила б сенс наявних посилань /items?status=.
    where, params = [], []
    if status:
        where.append("i.status = %s")
        params.append(status)
    if source_id:
        where.append("i.source_id = %s")
        params.append(source_id)
    if q:
        where.append("i.title ILIKE %s")
        params.append(f"%{q}%")
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT i.*, s.name AS source_name FROM seen_items i
              JOIN sources s ON s.id = i.source_id {clause}
             ORDER BY i.first_seen DESC LIMIT 50 OFFSET %s
            """,
            (*params, page * 50),
        ).fetchall()
        source_options = conn.execute(
            "SELECT id, name FROM sources ORDER BY name"
        ).fetchall()

    return templates.TemplateResponse(
        request,
        "items.html",
        {
            "nav": "items", "items": rows, "status": status, "source_id": source_id,
            "q": q, "page": page, "source_options": source_options,
        },
    )


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, mode: str = "", limit: int = 50):
    limit = 200 if limit == 200 else 50
    where = "WHERE mode = %s" if mode else ""
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT *, extract(epoch FROM (finished_at - started_at))::int AS duration_s
              FROM worker_runs {where}
             ORDER BY started_at DESC LIMIT {limit}
            """,
            (mode,) if mode else (),
        ).fetchall()
        modes = conn.execute(
            "SELECT DISTINCT mode FROM worker_runs ORDER BY mode"
        ).fetchall()
    return templates.TemplateResponse(
        request,
        "runs.html",
        {"nav": "runs", "runs": rows, "mode": mode, "limit": limit, "modes": modes},
    )


# ── Knowledge base ─────────────────────────────────────────────────


# Сентинели підсвітки — керуючі символи, а не «»: `ts_headline` мусить бути
# екранований разом із текстом поста (він може містити літеральний <script>),
# тож теги <mark> підставляє фільтр `hl` уже ПІСЛЯ екранування. Бонус:
# перестають ламатися легітимні лапки «» в українських постах.
HEADLINE_OPTS = "MaxWords=35, MinWords=15, StartSel=\x02, StopSel=\x03"


@app.get("/kb", response_class=HTMLResponse)
def kb_page(request: Request, q: str = "", forum: str = ""):
    with db() as conn:
        forums = conn.execute(
            """
            SELECT f.*, count(DISTINCT t.id) AS topics, count(p.id) AS posts,
                   max(t.bumped_at) AS newest_activity,
                   extract(day FROM now() - max(t.bumped_at))::int AS stale_days
              FROM kb.forums f
              LEFT JOIN kb.topics t ON t.forum_slug = f.forum_slug
              LEFT JOIN kb.posts p ON p.topic_ref = t.id
             GROUP BY f.id ORDER BY f.id
            """
        ).fetchall()

        results = []
        if q:
            results = conn.execute(
                """
                SELECT t.forum_slug, t.title, t.category_name,
                       t.url || '/' || p.post_number AS post_url,
                       p.author, p.posted_at,
                       -- Керуючі сентинели, не <b>: raw_text може містити
                       -- літеральний '<script>' (блоки коду на форумі,
                       -- сутності розкодовані на інжесті), тож шаблон
                       -- зобов'язаний екранувати сніпет — це робить фільтр hl.
                       ts_headline('english', p.raw_text,
                                   websearch_to_tsquery('english', %(q)s),
                                   %(opts)s) AS snippet
                  FROM kb.posts p JOIN kb.topics t ON t.id = p.topic_ref
                 WHERE p.body_tsv @@ websearch_to_tsquery('english', %(q)s)
                   AND (%(forum)s = '' OR t.forum_slug = %(forum)s)
                 ORDER BY ts_rank_cd(p.body_tsv,
                                     websearch_to_tsquery('english', %(q)s)) DESC
                 LIMIT 25
                """,
                {"q": q, "forum": forum, "opts": HEADLINE_OPTS},
            ).fetchall()

        recent_queries = conn.execute(
            "SELECT * FROM kb.query_log ORDER BY asked_at DESC LIMIT 10"
        ).fetchall()

    return templates.TemplateResponse(
        request,
        "kb.html",
        {
            "forums": forums, "q": q, "forum": forum,
            "results": results, "recent_queries": recent_queries,
        },
    )


@mutations.post("/kb/forums/{forum_id}/toggle")
def toggle_kb_forum(forum_id: int):
    with db() as conn:
        conn.execute(
            "UPDATE kb.forums SET enabled = NOT enabled WHERE id = %s", (forum_id,)
        )
        conn.commit()
    return RedirectResponse("/kb", status_code=303)


@mutations.post("/kb/forums/add")
def add_kb_forum(forum_slug: str = Form(...), base_url: str = Form(...)):
    """Register a forum for archiving. The actual crawl is `worker kb-backfill`
    (an overnight job, deliberately not a button — see User-Guide)."""
    if not base_url.startswith("https://"):
        return RedirectResponse("/kb", status_code=303)
    with db() as conn:
        conn.execute(
            "INSERT INTO kb.forums (forum_slug, base_url, enabled) VALUES (%s, %s, true) "
            "ON CONFLICT (forum_slug) DO UPDATE SET base_url = EXCLUDED.base_url, enabled = true",
            (forum_slug.strip().lower(), base_url.strip().rstrip("/")),
        )
        conn.commit()
    return RedirectResponse("/kb", status_code=303)


# ── Briefing packs ─────────────────────────────────────────────────

KBMCP_URL = os.environ.get("KBMCP_URL", "http://kbmcp:8000")
KB_MCP_TOKEN = os.environ.get("KB_MCP_TOKEN", "")


@mutations.post("/items/{item_uid}/brief")
def generate_brief(item_uid: str):
    """Manual trigger — the same call the n8n node makes after lead creation."""
    import httpx

    with db() as conn:
        item = conn.execute(
            """
            SELECT i.item_uid, i.title, s.ecosystem,
                   (SELECT l.payload->>'body' FROM items_log l
                     WHERE l.item_uid = i.item_uid AND l.event = 'fetched'
                     ORDER BY l.created_at DESC LIMIT 1) AS body
              FROM seen_items i JOIN sources s ON s.id = i.source_id
             WHERE i.item_uid = %s
            """,
            (item_uid,),
        ).fetchone()
    if not item:
        raise HTTPException(404, "item not found")

    try:
        response = httpx.post(
            f"{KBMCP_URL}/brief",
            json={
                "ecosystem": item["ecosystem"],
                "title": item["title"] or item_uid[:16],
                "body": item["body"] or "",
                "item_uid": item_uid,
            },
            headers={"Authorization": f"Bearer {KB_MCP_TOKEN}"} if KB_MCP_TOKEN else {},
            timeout=300,  # LLM tier legitimately takes minutes
        )
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return RedirectResponse(f"/items?status=&source_id=0#brief-error-{exc.__class__.__name__}",
                                status_code=303)

    if payload.get("error"):
        # No archive for this ecosystem — the honest outcome, show it inline.
        return RedirectResponse("/items", status_code=303)
    return RedirectResponse(f"/briefs/{payload['brief_id']}", status_code=303)


@app.get("/briefs", response_class=HTMLResponse)
def briefs_page(request: Request, ecosystem: str = "", tier: str = ""):
    """Список бріфів (розділ 4.10).

    Досі до бріфа вів лише редірект одразу після генерації — згенерований учора
    бріф знайти було неможливо.
    """
    where, params = [], []
    if ecosystem:
        where.append("ecosystem = %s")
        params.append(ecosystem)
    if tier:
        where.append("tier = %s")
        params.append(tier)
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    with db() as conn:
        briefs = conn.execute(
            f"SELECT id, ecosystem, title, tier, model, item_uid, created_at "
            f"FROM kb.briefs {clause} ORDER BY created_at DESC LIMIT 50",
            params,
        ).fetchall()
        ecosystems = conn.execute(
            "SELECT DISTINCT ecosystem FROM kb.briefs ORDER BY ecosystem"
        ).fetchall()

    return templates.TemplateResponse(
        request,
        "briefs.html",
        {
            "nav": "briefs",
            "briefs": briefs,
            "ecosystems": [r["ecosystem"] for r in ecosystems],
            "ecosystem": ecosystem,
            "tier": tier,
        },
    )


@app.get("/briefs/{brief_id}", response_class=HTMLResponse)
def view_brief(request: Request, brief_id: int):
    with db() as conn:
        brief = conn.execute(
            "SELECT * FROM kb.briefs WHERE id = %s", (brief_id,)
        ).fetchone()
    if not brief:
        raise HTTPException(404, "brief not found")
    return templates.TemplateResponse(
        request, "brief.html", {"nav": "briefs", "brief": brief}
    )


# ── AI-чат ─────────────────────────────────────────────────────────
#
# Історію пише kbmcp, не admin (розділ 4.9): POST /chat/send нижче лише
# проксує запит на {KBMCP_URL}/chat і читає готову відповідь — той самий
# поділ ролей, що й у generate_brief вище для kb.briefs (пише kbmcp, admin
# показує результат). Тому в цьому розділі рівно один SELECT і жодного
# INSERT: права БД admin на kb.* — читання (див. kb_page, briefs_page).


def _chat_key(request: Request) -> str:
    """→ ключ сесії БЕЗ namespace 'web:' (його додає виклик, симетрично до
    того, як kbmcp додає його на записі — розділ 4.9, контракт /chat).

    Легасі cookie (видані до появи sid в admin/auth.py, `session_sid`
    повертає '') отримують стабільний ключ за підписом людини: без цього
    кожен re-sign (а він трапляється на КОЖНІЙ відповіді) губив би історію,
    бо сам sid у такої cookie відсутній за визначенням. Без двокрапки — вона
    вже є в самому namespace-префіксі, дві підряд ускладнили б парсинг на
    боці kbmcp без жодної користі.
    """
    who = auth.session_who(request)
    sid = auth.session_sid(request)
    return sid if sid else f"legacy-{who}"


def _chat_backend(payload: dict) -> dict:
    """Виокремлено в окрему функцію, а не інлайн у хендлері (на відміну від
    generate_brief вище) — саме для того, щоб тести підміняли її
    monkeypatch'ем, не піднімаючи kbmcp і не ходячи в мережу.
    """
    import httpx

    response = httpx.post(
        f"{KBMCP_URL}/chat",
        json=payload,
        headers={"Authorization": f"Bearer {KB_MCP_TOKEN}"} if KB_MCP_TOKEN else {},
        timeout=300,  # LLM tool loops легітимно тривають хвилини — те саме, що й /brief вище
    )
    return response.json()


@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request, error: str = ""):
    """AI-чат над базою знань (розділ 4.9, задача #30)."""
    who = auth.session_who(request)
    session_key = f"web:{_chat_key(request)}"

    with db() as conn:
        rows = conn.execute(
            "SELECT id, role, who, content, tier, model, created_at "
            "FROM kb.chat_messages WHERE session_key = %s ORDER BY id LIMIT 200",
            (session_key,),
        ).fetchall()

    # Останній рядок — user: відповідь, можливо, ще генерується (POST
    # /chat/send блокується до 300 с — той самий таймаут, що й у /brief) в
    # ІНШІЙ вкладці чи запиті, який саме зараз обробляється. Рядок-натяк, а
    # не спінер із поллінгом — автополінг заборонений (app.js, розділ 0):
    # він тихо продовжував би сесію без участі людини.
    thinking = bool(rows) and rows[-1]["role"] == "user"

    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "nav": "chat",
            "who": who,
            "messages": rows,
            "thinking": thinking,
            "error": error,
        },
    )


@mutations.post("/chat/send")
def send_chat_message(request: Request, message: str = Form(...)):
    """Одне повідомлення → один запит до kbmcp.

    Відповідь гілкується заголовком X-Requested-With: fetch — його ставить
    лише app.js (звичайний браузер його не додає), тож без JS форма завжди
    йде по PRG-контракту (303 на /chat), а з JS повертається JSON. Помилки
    kbmcp НЕ ковтаються мовчки в редірект без деталей (на відміну від
    generate_brief — там немає JS-гілки, і фрагмент у URL прийнятний
    компроміс): тут є куди показати причину, і ковтати її означало б
    видавати порожню відповідь замість пояснення.
    """
    # СИРИЙ ключ, без префікса "web:" — kbmcp неймспейсить сам при записі
    # (контракт /chat забороняє ':' у session_key саме для того, щоб веб не
    # міг адресувати telegram-сесії). Префікс додається лише при ЧИТАННІ
    # kb.chat_messages у chat_page — там ключ уже збережений неймспейснутим.
    session_key = _chat_key(request)
    who = auth.session_who(request)
    is_fetch = request.headers.get("X-Requested-With") == "fetch"

    def fail(err: str, status_code: int = 400):
        if is_fetch:
            return JSONResponse({"ok": False, "error": err}, status_code=status_code)
        return RedirectResponse(f"/chat?{urlencode({'error': err})}", status_code=303)

    text = message.strip()
    if not text:
        return fail("Message is empty")
    if len(text) > 4000:
        return fail(f"Message is too long — {len(text)} characters (limit is 4000)")

    import httpx

    try:
        payload = _chat_backend(
            {"channel": "web", "session_key": session_key, "who": who, "message": text}
        )
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("kbmcp /chat unreachable: %s", exc)
        return fail("Chat backend is unreachable — please try again in a moment", 502)

    if not payload.get("ok"):
        return fail(payload.get("error") or "Chat backend returned an error")

    if is_fetch:
        return JSONResponse(
            {
                "ok": True,
                "reply": payload.get("reply_md", ""),
                "tier": payload.get("tier"),
                "model": payload.get("model"),
            }
        )
    return RedirectResponse("/chat", status_code=303)


@mutations.post("/chat/new")
def new_chat(request: Request):
    """«Новий чат»: ротація sid (admin/auth.py) — той самий who, свіжа
    історія. `/chat/new` навмисно в auth.NO_REISSUE (див. коментар там):
    інакше rolling re-sign у middleware ПІСЛЯ цього хендлера перевидав би
    cookie зі СТАРИМ sid, прочитаним із request.cookies (Set-Cookie цієї
    відповіді йому не видно), і ротація не мала б жодного ефекту."""
    response = RedirectResponse("/chat", status_code=303)
    auth.issue(response, request, rotate_sid=True)
    return response


# Реєстрація мутуючого роутера — ОСТАННІМ рядком серед маршрутів, після того як
# усі @mutations.post оголошені: include_router копіює наявні на той момент
# роути, а не тримає посилання на роутер. Виклик вище зареєстрував би порожній
# набір, і кожна форма віддавала б 404 — саме це і сталося на кроці 2.
app.include_router(mutations)


# ── Mock n8n (local dev only) ──────────────────────────────────────


@app.post("/mock/webhook")
async def mock_webhook(request: Request):
    """Stand-in for the n8n pipeline in local dev.

    Same contract as the real thing: secret header required, and 'done' is
    written only at the end — so the worker's at-least-once loop is exercised
    exactly as it will be in production (A1).
    """
    if not MOCK_N8N:
        raise HTTPException(404)
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET or not WEBHOOK_SECRET:
        raise HTTPException(401, "bad or missing X-Webhook-Secret")

    payload = await request.json()
    item_uid = payload.get("item_uid")
    if not item_uid:
        raise HTTPException(422, "item_uid missing")

    with db() as conn:
        conn.execute(
            "INSERT INTO items_log (item_uid, source_id, event, payload) "
            "VALUES (%s, %s, 'mock_delivered', %s)",
            (item_uid, payload.get("source_id"), json.dumps({"title": payload.get("title")})),
        )
        updated = conn.execute(
            "UPDATE seen_items SET status = 'done', delivered_at = now() "
            "WHERE item_uid = %s AND status = 'pending' RETURNING item_uid",
            (item_uid,),
        ).fetchone()
        conn.commit()

    log.info("mock n8n confirmed %s (%s)", item_uid, payload.get("title", "")[:60])
    return {"ok": True, "confirmed": bool(updated)}
