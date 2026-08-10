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

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

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

# kbmcp — тут, а не поруч із першим використанням: раніше жив біля
# generate_brief (розділ «Briefing packs»), але тепер його читають три
# незв'язані секції (keywords advice, briefs, AI-чат) — спільна конфігурація
# належить головному конфіг-блоку, а не одній із трьох.
KBMCP_URL = os.environ.get("KBMCP_URL", "http://kbmcp:8000")
KB_MCP_TOKEN = os.environ.get("KB_MCP_TOKEN", "")

STATIC = Path(__file__).parent / "static"

# Кеш-бастинг: хеш ВМІСТУ обох асетів на імпорті → ?v= у base.html і
# login.html. Було mtime лише app.css — і js-only правка (фікс FormData
# 2026-08-07) не міняла версію, тож браузери законно виконували старий
# app.js попри свіжий деплой. Вміст, а не mtime: docker COPY і git clone
# виставляють mtime непередбачувано, а хеш бреше тільки якщо збрехали файли.
try:
    _asset_hash = hashlib.sha1()
    for _asset_name in ("app.css", "app.js"):
        _asset_hash.update((STATIC / _asset_name).read_bytes())
    ASSET_V: int | str = _asset_hash.hexdigest()[:10]
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
# Win/lost-фідбек на /items (розділ «Знахідки», задача розширення). 'open' —
# лише значення ФІЛЬТРА (доставлено, вердикту ще нема); чипа на рядку для
# нього немає — рядок без вердикту показує самі кнопки Won/Lost.
OUTCOME_UA = {"won": "виграно", "lost": "програно", "open": "очікує"}


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
    """Відповідь AI-чату (розділ 4.9): markdown НЕ рендериться (чат — живий
    діалог, а не документ, і сирий URL у відповіді моделі — єдине, що
    потребує підсвітки), але голий URL має бути клікабельним. Для brief.html
    — окремий, ширший рендерер `md_lite` нижче: бріф читають і зберігають,
    тож форматування (заголовки, списки, посилання) там виправдане, а тут —
    зайве.

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


# ── md_lite: безпечний підмножина-markdown для brief.html ────────────
#
# Той самий принцип «спершу escape() ВСЬОГО, потім підставляємо теги», що й у
# hl()/linkify() вище — і причина та сама: brief_md — вихід LLM (kbmcp), тобто
# недовірений текст, який може містити буквальний `<script>` чи посилання на
# `javascript:...`. Один `escape()` на вході прибирає РІВНО загрозу «сирий
# HTML проліз» — після нього в тексті фізично не лишається жодного `<`/`>`,
# отже жоден наступний регексп не може відтворити тег, якого не було в нашому
# власному шаблоні заміни. Далі рендеряться ЛИШЕ перелічені нижче конструкції;
# усе інше лишається escaped-текстом як є (без сюрпризів у вигляді розкиданих
# `**`/`[...]`, які «майже» схожі на markdown).
#
# Свідомо НЕ підтримується: вкладені *bold*/*italic*, нумеровані списки,
# таблиці, зображення, будь-який сирий HTML — це «lite», а не CommonMark.
#
# `##`/`###` — ОБИДВА рівні, перевірено на живих даних (mcp/briefing.py):
# basic-рівень пише РІВНО один `## KB brief: …` на бріф, LLM-рівень (system
# prompt там-таки) — кілька `### Розділ` без жодного `##`. Оскільки рівні
# ніколи не змішуються в одному бріфі, обидва мапляться в один <h3> — під
# заголовком екосистеми (`<h2>`) на самій сторінці (brief.html) різниці не
# видно, а окремий <h2>/<h3> тут дав би розбіжну ієрархію без жодної користі.
_MD_HEADING_RE = re.compile(r"^#{2,3} (.*)$")
_MD_BULLET_RE = re.compile(r"^- (.*)$")
_MD_INLINE_RE = re.compile(
    r"\[(?P<label>[^\[\]]{1,200})\]\((?P<href>https://[^\s()<>]+)\)"
    r"|\*\*(?P<bold>[^*\n]+)\*\*"
    r"|\*(?P<italic>[^*\n]+)\*"
    # `_italic_` — теж живі дані: mcp/briefing.py закриває кожен basic-бріф
    # рядком `_Auto-generated from the forum archive (…)_`. Межі слова
    # (`(?<!\w)`/`(?!\w)`) обов'язкові: без них `ANTHROPIC_API_KEY` у ТІЙ
    # самій нотатці сам стає парою підкреслень і розвалюється на
    # `ANTHROPIC<em>API</em>KEY` — знайдено наживо на реальному бріфі з
    # docker compose, не вигадано. З межами слова snake_case ідентифікатор
    # просто не збігається як роздільник (безпечний відкат до літерального
    # тексту), а справжній `_italic_` — так.
    r"|(?<!\w)_(?P<italic_u>\S(?:[^_\n]*\S)?)_(?!\w)"
    r"|(?P<url>https?://[^\s<>\"']+)"
)


def _md_inline(escaped_line: str) -> str:
    """Інлайн-розмітка ВСЕРЕДИНІ одного рядка, який уже пройшов escape().

    Один комбінований регексп з альтернативами замість кількох послідовних
    `.sub()` — інакше перший прохід (скажімо, bare-URL autolink) вставив би
    `<a href="...">`, і другий прохід (bold) побачив би вже готовий тег і міг
    би зламати його чи задвоїти посилання всередині власного `<a>`. Порядок
    альтернатив важливий: markdown-посилання `[label](https://...)` — ПЕРЕД
    голим `https://` нижче, інакше URL усередині дужок з'їсть саму
    альтернативу посилання.
    """

    def repl(m: re.Match) -> str:
        if m.group("label") is not None:
            return (
                f'<a href="{m.group("href")}" target="_blank" rel="noopener">'
                f'{m.group("label")}</a>'
            )
        if m.group("bold") is not None:
            return f"<strong>{m.group('bold')}</strong>"
        if m.group("italic") is not None:
            return f"<em>{m.group('italic')}</em>"
        if m.group("italic_u") is not None:
            return f"<em>{m.group('italic_u')}</em>"
        url = m.group("url")
        return f'<a href="{url}" target="_blank" rel="noopener">{url}</a>'

    return _MD_INLINE_RE.sub(repl, escaped_line)


def md_lite(text: str) -> Markup:
    """Безпечний підмножина-рендерер для `kb.briefs.brief_md` (розділ 4.8).

    Підтримується: `##`/`### ` заголовки, `**bold**`, `*italic*`/`_italic_`,
    `- ` списки, `[текст](https://...)` посилання (лише https —
    javascript:/data: ніколи не стають клікабельними, бо просто не
    збігаються з патерном), голі URL
    автопосилаються, абзаци розділяються порожнім рядком. Жодного сирого
    HTML і жодних зображень — свідомо (розділ 4.8, той самий compromise, що
    в hl()/linkify() вище, лише ширший словник дозволених конструкцій).
    """
    escaped = str(escape(text or ""))
    lines = escaped.split("\n")

    html_parts: list[str] = []
    para_buf: list[str] = []
    list_buf: list[str] = []

    def flush_para() -> None:
        if para_buf:
            html_parts.append(
                "<p>" + "<br>".join(_md_inline(ln) for ln in para_buf) + "</p>"
            )
            para_buf.clear()

    def flush_list() -> None:
        if list_buf:
            items = "".join(f"<li>{_md_inline(li)}</li>" for li in list_buf)
            html_parts.append(f"<ul>{items}</ul>")
            list_buf.clear()

    for raw_line in lines:
        line = raw_line.rstrip("\r")
        heading = _MD_HEADING_RE.match(line)
        bullet = _MD_BULLET_RE.match(line)
        if heading:
            flush_para()
            flush_list()
            html_parts.append(f"<h3>{_md_inline(heading.group(1))}</h3>")
        elif bullet:
            flush_para()
            list_buf.append(bullet.group(1))
        elif line.strip() == "":
            flush_para()
            flush_list()
        else:
            flush_list()
            para_buf.append(line)
    flush_para()
    flush_list()

    return Markup("".join(html_parts))


templates.env.filters["hl"] = hl
templates.env.filters["linkify"] = linkify
templates.env.filters["md_lite"] = md_lite
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
    OUTCOME_UA=OUTCOME_UA,
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


def _render_keywords(
    request: Request,
    *,
    message: str = "",
    error: str = "",
    advice_md: str = "",
    advice_model: str = "",
    status_code: int = 200,
):
    """Одна точка рендеру /keywords — і GET, і POST /keywords/advice нижче
    малюють ту саму сторінку (розділ A): порада від AI не має власного URL, і
    жоден із двох хендлерів не повинен дублювати SELECT/розбивку на панелі.
    """
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
            "advice_md": advice_md,
            "advice_model": advice_model,
        },
        status_code=status_code,
    )


@app.get("/keywords", response_class=HTMLResponse)
def keywords_page(request: Request, message: str = "", error: str = ""):
    return _render_keywords(request, message=message, error=error)


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


def _keywords_advice_backend() -> dict:
    """Виокремлено в окрему функцію (той самий прийом, що й `_chat_backend`
    нижче) — саме для того, щоб тести підміняли її monkeypatch'ем, не
    піднімаючи kbmcp і не ходячи в мережу. Порожнє тіло `{}`: kbmcp сам читає
    поточні keywords/статистику знахідок із БД — запиту нема чого передавати.
    """
    import httpx

    response = httpx.post(
        f"{KBMCP_URL}/keywords-advice",
        json={},
        headers={"Authorization": f"Bearer {KB_MCP_TOKEN}"} if KB_MCP_TOKEN else {},
        timeout=300,  # LLM-рівень легітимно триває хвилини — той самий контракт, що й /chat, /brief
    )
    return response.json()


@mutations.post("/keywords/advice")
def keywords_advice(request: Request):
    """AI-помічник для ключових слів (розділ A): кнопка «Suggest keywords
    (AI)» на /keywords викликає kbmcp, той дивиться на поточний список
    include/exclude і на статистику знахідок і повертає готовий текст поради.

    PRG тут СВІДОМО зламаний, на відміну від add_keyword/toggle_keyword вище:
    порада — ефемерна відповідь LLM, а не стан, який варто зберігати. Ані
    сесійного сховища, ані таблиці під це немає — і не мало б бути: сторінка
    рендериться НАПРЯМУ з цього POST-хендлера (та сама _render_keywords, що й
    у GET /keywords), з порадою прямо в контексті. Оновлення сторінки
    природно її прибирає — це і є інтуїтивна семантика «згенерувати ще раз»,
    а зберігати одноразову відповідь заради пережиття F5 — стан заради стану.
    """
    import httpx

    tr = i18n.translator(i18n.lang_of(request))

    try:
        payload = _keywords_advice_backend()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("kbmcp /keywords-advice unreachable: %s", exc)
        return _render_keywords(
            request,
            error=tr(
                "pg.keywords.advice_unreachable",
                "Could not reach the AI advice backend — try again in a moment",
            ),
            status_code=502,
        )

    if not payload.get("ok"):
        log.warning("kbmcp /keywords-advice returned an error: %s", payload.get("error"))
        return _render_keywords(
            request,
            error=tr(
                "pg.keywords.advice_error",
                "Could not generate keyword suggestions right now — try again in a moment",
            ),
            status_code=503,
        )

    return _render_keywords(
        request,
        advice_md=payload.get("advice_md", ""),
        advice_model=payload.get("model", ""),
    )


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
    ("ai", "AI chat and briefs"),
    ("other", "Other"),
]

# Підказки навмисно чесні (розділ 7.12): кілька ключів зараз не читає ніхто,
# і підказка, що описує неіснуючу поведінку, гірша за її відсутність.
#
# Кожен запис несе ДВІ різні за призначенням підказки (запит Миколи: «нічого
# не ясно, потрібні підказки і рекомендації», розділ A):
#   help — що робить ключ і що станеться, якщо його підняти/опустити;
#   reco — коротке рекомендоване значення чи діапазон, окремо від help, щоб
#          його можна було показати як самостійну пігулку, а не ховати в
#          середині речення.
SETTING_META = {
    "confidence_threshold": {
        "group": "classify", "label": "Auto-lead threshold", "type": "float",
        "help": "Confidence score above which a finding becomes a lead automatically, "
                "with no human review. Raising it cuts false positives at the cost of "
                "missing some real leads; lowering it catches more leads at the cost "
                "of noise reaching the team. Read by n8n (rfp-main).",
        "reco": "0.7–0.8 is a reasonable start — tune it using Won/Lost outcomes on the Findings page.",
    },
    "review_band_low": {
        "group": "classify", "label": "Review band, lower bound", "type": "float",
        "help": "Lower edge of the manual-review band: findings scoring between this "
                "value and the auto-lead threshold are queued for a human instead of "
                "being auto-approved or silently dropped. Raising it shrinks the "
                "review band; lowering it grows the review queue.",
        "reco": "Keep it comfortably below the auto-lead threshold — 0.3–0.5 is typical.",
    },
    "classifier_prompt_version": {
        "group": "classify", "label": "Prompt version", "type": "text",
        "help": "Free-text label for which classifier prompt produced a verdict — "
                "useful for audits once the real classifier is live. In STUB mode the "
                "classifier ignores this and writes its own placeholder value.",
        "reco": "Leave it as-is until the classifier is wired to read it.",
    },
    "max_leads_per_run": {
        "group": "volume", "label": "Max leads per run", "type": "int",
        "min": 1, "max": 500,
        "help": "Upper bound on how many leads a single worker run may create. Not in "
                "use yet — no component reads this key, so changing it has no effect today.",
        "reco": "Leave the default (25) — revisit once a component reads this key.",
    },
    "lead_floor_7d": {
        "group": "volume", "label": "Minimum leads per 7 days", "type": "int",
        "min": 0, "max": 100,
        "help": "Minimum number of leads expected in a rolling 7-day window; n8n's "
                "digest job alerts when the real count falls under it — often the "
                "first sign a source went quiet, before anything actually errors. "
                "Raise it to get alerted sooner, lower it (or 0) to quiet the alert.",
        "reco": "3–5 works for a small source list; raise it as more sources come online.",
    },
    "source_dark_days": {
        "group": "volume", "label": "Days of source silence before an alert", "type": "int",
        "min": 1, "max": 365,
        "help": "Days a source can go silent (no new item) before it's flagged as gone "
                "dark. The worker reads this on every run — a non-numeric value here "
                "stops the run mid-cycle.",
        "reco": "14 is a solid default; lower it for sources that post daily, raise it "
                "for slow-moving ones.",
    },
    "alert_channel": {
        "group": "channels", "label": "Alerts channel", "type": "channel",
        "help": "Slack channel name for operational alerts (source failures, dark "
                "sources). Not in use yet — Slack nodes are disabled, notifications "
                "currently go to Telegram instead.",
        "reco": "Leave the default unless Slack nodes get re-enabled.",
    },
    "review_channel": {
        "group": "channels", "label": "Review channel", "type": "channel",
        "help": "Slack channel name for findings that land in the review band "
                "(between the two thresholds above). Not in use yet (Slack nodes are disabled).",
        "reco": "Leave the default unless Slack nodes get re-enabled.",
    },
    "digest_channel": {
        "group": "channels", "label": "Digest channel", "type": "channel",
        "help": "Slack channel name for the periodic digest of everything that wasn't "
                "auto-delivered as a lead. Not in use yet (Slack nodes are disabled).",
        "reco": "Leave the default unless Slack nodes get re-enabled.",
    },
    "lead_title_template": {
        "group": "leads", "label": "Lead title template", "type": "text",
        "help": "Template for the lead's title downstream, with placeholders {label}, "
                "{ecosystem}, {title}. Not in use yet — no component reads this key today.",
        "reco": "Leave the default; revisit once lead titles are templated.",
    },
    "chat_model": {
        "group": "ai", "label": "Chat model", "type": "text",
        "help": "Anthropic model id used by the LLM tier of the KB chat (dashboard and "
                "Telegram). Takes effect on the very next message — no redeploy needed. "
                "An unknown model id makes every chat request fail.",
        "reco": "claude-sonnet-5 is a good cost/quality balance; use claude-opus-5 only "
                "if answers need to be noticeably sharper.",
    },
    "chat_daily_token_budget": {
        "group": "ai", "label": "Chat daily token budget", "type": "int",
        "min": 1000, "max": 5_000_000,
        "help": "Global daily cap on tokens_in + tokens_out across ALL chat sessions "
                "combined (web and Telegram, not per session). Once the day's usage "
                "passes this number, chat answers quietly degrade to the keyword tier "
                "until midnight instead of failing outright. Raising it keeps "
                "LLM-quality answers flowing longer each day at higher cost; lowering "
                "it saves cost at the price of an earlier fallback.",
        "reco": "300000 is the current default — raise it if the team routinely hits "
                "the keyword-tier fallback before end of day.",
    },
    "brief_model": {
        "group": "ai", "label": "Brief model", "type": "text",
        "help": "Anthropic model id used by the LLM tier when generating a brief from "
                "the knowledge base. Takes effect without a redeploy; an unknown model "
                "id makes brief generation fail.",
        "reco": "claude-opus-5 is the current default — briefs are infrequent enough "
                "that the stronger model is worth the extra cost.",
    },
    "brief_language": {
        "group": "ai", "label": "Brief language", "type": "text",
        "help": "Output language for generated briefs, handed to the sales team. Only "
                "affects the text inside generated briefs, not the dashboard UI language.",
        "reco": "'en' unless the team reading the briefs prefers another language.",
    },
    # З'являється рядком у `settings` після міграції 009 (kbmcp-сторона,
    # паралельна робота) — до того просто не рендериться (SETTING_META з
    # ключем без відповідного рядка в БД нікому не заважає).
    "chat_web_search": {
        "group": "ai", "label": "Chat web search", "type": "bool",
        "help": "Allows the chat agent to also search the live internet, not just the "
                "archived forums, when answering a question. Searches are billed "
                "separately by Anthropic, on top of the usual per-message token cost.",
        "reco": "Off until you want fresher-than-archive answers — most questions are "
                "already covered by the forum archive.",
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
    elif kind == "bool":
        if value not in ("on", "off"):
            return False, f"{label}: expected 'on' or 'off', got “{value}”"
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
        item["help"] = tr(f"set.{row['key']}.help", meta.get("help", "")) if meta.get("help") else ""
        item["reco"] = tr(f"set.{row['key']}.reco", meta.get("reco", "")) if meta.get("reco") else ""
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

# Фіксовані опції — рендеряться як <select>, тож значення з довільного тексту
# формою не приходять; будь-яке інше значення в query-рядку просто ігнорується
# (не 400 — старе посилання з відкликаною опцією має показати «без фільтра»,
# а не зламатись).
CONFIDENCE_OPTIONS = ("0.5", "0.7", "0.9")
PERIOD_OPTIONS = ("7d", "30d")
OUTCOME_OPTIONS = ("won", "lost", "open")


def _ilike_term(raw: str) -> str:
    """Екранує `%`/`_` (і сам `\\`) ПЕРЕД тим, як обгорнути в `%…%` — інакше
    заголовок із буквальним `%` чи `_` тихо перетворює пошук на неочікуваний
    wildcard-збіг. Postgres LIKE/ILIKE за замовчуванням і так використовує
    `\\` як escape-символ, тож окремий `ESCAPE '\\'` у запиті не потрібен."""
    escaped = raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _ask_ai_question(title: str | None, ecosystem: str | None, item_uid: str) -> str:
    """Англійське питання для «Ask AI» на /items (розділ D3) — прив'язане до
    конкретної знахідки, а не голе посилання на /chat.

    Будується тут, а не в шаблоні: шаблон лишається чистою презентацією, а
    обрізання довгого заголовка — логіка, не розмітка. Англійською навмисно
    (той самий інваріант, що й приклади в chat.html): чат-бекенд відмовляється
    шукати за не-латинським запитом (комміт «Stub chat: refuse non-Latin
    questions»), тож питання, зібране з українського заголовка знахідки,
    мусить лишатися латиницею — сама структура речення вже така.
    """
    label = (title or item_uid[:16]).strip()
    if len(label) > 100:
        label = label[:99].rstrip() + "…"
    eco = ecosystem or "this ecosystem"
    return (
        f'What has {eco} funded similar to "{label}"? '
        "Who decides and what are typical amounts?"
    )


@app.get("/items", response_class=HTMLResponse)
def items_page(
    request: Request,
    status: str = "",
    q: str = "",
    ecosystem: str = "",
    lane: str = "",
    min_confidence: str = "",
    period: str = "",
    outcome: str = "",
    page: int = 0,
):
    """Знахідки (розділ 4.3, розширення «фільтри + outcomes»).

    Дефолт «усі» НЕ змінюється (F5, той самий інваріант, що й раніше): коли
    жоден параметр не заданий, WHERE лишається порожнім і запит — той самий,
    що й до цієї зміни, тож наявні закладки/посилання з Telegram (`?status=`)
    показують те саме, що й учора.
    """
    where, params = [], []
    if status:
        where.append("i.status = %s")
        params.append(status)
    if q:
        where.append("i.title ILIKE %s")
        params.append(_ilike_term(q))
    if ecosystem:
        where.append("s.ecosystem = %s")
        params.append(ecosystem)
    if lane:
        where.append("s.lane = %s")
        params.append(lane)
    if min_confidence in CONFIDENCE_OPTIONS:
        where.append("i.confidence >= %s")
        params.append(float(min_confidence))
    if period == "7d":
        where.append("i.first_seen > now() - interval '7 days'")
    elif period == "30d":
        where.append("i.first_seen > now() - interval '30 days'")
    if outcome == "won":
        where.append("i.outcome = 'won'")
    elif outcome == "lost":
        where.append("i.outcome = 'lost'")
    elif outcome == "open":
        where.append("i.delivered_at IS NOT NULL AND i.outcome IS NULL")
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    # Хвіст запиту БЕЗ page — для лінків «новіші/старіші» і для «Скинути».
    # Лише активні фільтри: порожній параметр не повинен смітити URL.
    active_filters = {
        k: v
        for k, v in {
            "status": status, "q": q, "ecosystem": ecosystem, "lane": lane,
            "min_confidence": min_confidence, "period": period, "outcome": outcome,
        }.items()
        if v
    }
    qs_no_page = urlencode(active_filters)

    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT i.*, s.name AS source_name, s.ecosystem AS source_ecosystem
              FROM seen_items i JOIN sources s ON s.id = i.source_id {clause}
             ORDER BY i.first_seen DESC LIMIT 50 OFFSET %s
            """,
            (*params, page * 50),
        ).fetchall()
        ecosystem_options = conn.execute(
            "SELECT DISTINCT ecosystem FROM sources ORDER BY ecosystem"
        ).fetchall()

    # «Ask AI» (розділ D3): питання будується тут, не в шаблоні — див. docstring
    # _ask_ai_question. quote(..., safe="") замість urlencode(), бо це один
    # рядок тексту, а не словник пар ключ=значення.
    for row in rows:
        row["ask_ai_href"] = "/chat?ask=" + quote(
            _ask_ai_question(row["title"], row["source_ecosystem"], row["item_uid"]),
            safe="",
        )

    return templates.TemplateResponse(
        request,
        "items.html",
        {
            "nav": "items", "items": rows,
            "status": status, "q": q, "ecosystem": ecosystem, "lane": lane,
            "min_confidence": min_confidence, "period": period, "outcome": outcome,
            "page": page,
            "ecosystem_options": [r["ecosystem"] for r in ecosystem_options],
            "filters_active": bool(active_filters),
            "qs_no_page": qs_no_page,
        },
    )


@mutations.post("/items/{item_uid}/outcome")
def set_item_outcome(request: Request, item_uid: str, outcome: str = Form(...), qs: str = Form("")):
    """Won/Lost/«очистити» на /items (розділ 4.3). Без цього зворотного
    зв'язку нема на чому калібрувати confidence_threshold/review_band_low —
    команда бачить лише «лід створено», а не «лід був вартий створення».

    `qs` — поточний рядок запиту сторінки /items (hidden-поле форми, розділ
    4.3): PRG повертає туди ж, звідки прийшли, а не на дефолтний /items без
    фільтрів — інакше кожен клік Won/Lost «загублював» би застосовані фільтри.
    """
    if outcome not in ("won", "lost", "clear"):
        raise HTTPException(400, "outcome must be 'won', 'lost' or 'clear'")

    with db() as conn:
        if outcome == "clear":
            conn.execute(
                "UPDATE seen_items SET outcome = NULL, outcome_by = NULL, "
                "outcome_at = NULL WHERE item_uid = %s",
                (item_uid,),
            )
        else:
            conn.execute(
                "UPDATE seen_items SET outcome = %s, outcome_by = %s, outcome_at = now() "
                "WHERE item_uid = %s",
                (outcome, auth.actor(request), item_uid),
            )
        conn.commit()

    # `qs` — тільки рядок запиту (без "?"), тож ціль завжди лишається на
    # /items незалежно від того, що прийшло у формі — відкритого редіректу
    # тут немає навіть у теорії. \r\n на випадок ручного/зіпсованого POST.
    safe_qs = qs.replace("\r", "").replace("\n", "")[:2000]
    target = f"/items?{safe_qs}" if safe_qs else "/items"
    return RedirectResponse(target, status_code=303)


# ── Collection log — деталізована діагностика (розділ C) ─────────────
#
# «Що робити з помилками», детерміновано: список рядків worker_runs.detail
# ->'failures' (worker/pipeline.py: f"{source.name}: {ExcType} — {exc}") —
# сирий текст винятку, зрозумілий інженеру, але не тому, хто веде дашборд.
# ШІ-діагностика — пізніше; тут — чисті регекси без БД і без мережі, тож їх
# легко юніт-тестувати (admin/tests) і легко розширювати новим правилом.
#
# Порядок ПРАВИЛ важливий: перший збіг перемагає (diagnose_error нижче йде
# зверху вниз), тож специфічніші коди (429/401/403/404) стоять ПЕРЕД
# загальнішими патернами (timeout, format) — інакше рядок на кшталт
# "GET ... returned 404 (timeout waiting for retry)" міг би впасти не в те
# правило залежно від порядку.
DIAG_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b429\b|rate[- ]?limit", re.IGNORECASE), "rate_limited"),
    (re.compile(r"\b401\b|\b403\b", re.IGNORECASE), "auth"),
    (re.compile(r"\b404\b", re.IGNORECASE), "not_found"),
    (re.compile(r"\btimeout\b|connecttimeout|readtimeout|writetimeout|pooltimeout",
                re.IGNORECASE), "timeout"),
    (re.compile(r"gaierror|name resolution|name or service not known|"
                r"nodename nor servname|temporary failure in name resolution",
                re.IGNORECASE), "dns"),
    (re.compile(r"ssrfblocked|непублічну адресу", re.IGNORECASE), "ssrf"),
    (re.compile(r"jsondecodeerror|json\.decoder|invalid json|expecting value|"
                r"unexpected format", re.IGNORECASE), "format"),
    (re.compile(r"\bquarantined\b|consecutive failures", re.IGNORECASE), "quarantined"),
]
DIAG_FALLBACK = "generic"

# Англійська тут (як і в SETTING_META вище) — дефолт для t(), українська —
# ключем diag.<key> в i18n.UK.
DIAG_ADVICE_EN: dict[str, str] = {
    "rate_limited": "Source is rate-limiting us; the worker already backs off — if "
                     "it persists for days, lower this source's frequency or check "
                     "API key limits.",
    "auth": "Key or access problem — check this source's credential in .env.",
    "not_found": "Source URL changed — re-verify it via the Sources page test-fetch.",
    "timeout": "Source is slow or unreachable right now — usually transient.",
    "dns": "Domain doesn't resolve — the source may be dead, or this is a "
           "temporary DNS hiccup.",
    "ssrf": "URL resolves to a private, non-public address — deliberately blocked "
            "(SSRF guard), not a bug to retry.",
    "format": "Source changed its response format — the config mapping needs an update.",
    "quarantined": "Source auto-paused after repeated failures — fix the cause, "
                    "then re-enable it from the Sources page.",
    DIAG_FALLBACK: "Re-run the test-fetch on the Sources page to see the current error live.",
}


def diagnose_error(text: str) -> str:
    """→ diag key для одного рядка з worker_runs.detail['failures'].

    Чиста функція без БД (юніт-тестується напряму): перше правило, що
    збіглося, перемагає — див. коментар над DIAG_RULES про порядок.
    """
    for pattern, key in DIAG_RULES:
        if pattern.search(text):
            return key
    return DIAG_FALLBACK


templates.env.filters["diag_key"] = diagnose_error
templates.env.globals["DIAG_ADVICE_EN"] = DIAG_ADVICE_EN


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
def briefs_page(request: Request, ecosystem: str = "", tier: str = "", archived: int = 0):
    """Список бріфів (розділ 4.10, розширення «lifecycle»).

    Досі до бріфа вів лише редірект одразу після генерації — згенерований учора
    бріф знайти було неможливо.

    Заархівовані ховаються за замовчуванням (`archived=0`): архів — те саме
    «прибрати з-перед очей», що й `enabled=false` на /sources, а не видалення,
    тож список без перемикача лишається таким, яким команда його й звикла
    бачити.
    """
    where, params = [], []
    if not archived:
        where.append("archived_at IS NULL")
    if ecosystem:
        where.append("ecosystem = %s")
        params.append(ecosystem)
    if tier:
        where.append("tier = %s")
        params.append(tier)
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    with db() as conn:
        briefs = conn.execute(
            f"SELECT id, ecosystem, title, tier, model, item_uid, created_at, archived_at "
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
            "archived": bool(archived),
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


@mutations.post("/briefs/{brief_id}/archive")
def archive_brief(brief_id: int):
    with db() as conn:
        conn.execute(
            "UPDATE kb.briefs SET archived_at = now() WHERE id = %s AND archived_at IS NULL",
            (brief_id,),
        )
        conn.commit()
    return RedirectResponse(f"/briefs/{brief_id}", status_code=303)


@mutations.post("/briefs/{brief_id}/unarchive")
def unarchive_brief(brief_id: int):
    with db() as conn:
        conn.execute(
            "UPDATE kb.briefs SET archived_at = NULL WHERE id = %s", (brief_id,)
        )
        conn.commit()
    return RedirectResponse(f"/briefs/{brief_id}", status_code=303)


@mutations.post("/briefs/{brief_id}/delete")
def delete_brief(brief_id: int):
    """Безповоротно (розділ 4.8): підтвердження — на клієнті через
    `data-confirm` (app.js, той самий ідіом, що й вимкнення джерела/форуму).
    """
    with db() as conn:
        conn.execute("DELETE FROM kb.briefs WHERE id = %s", (brief_id,))
        conn.commit()
    return RedirectResponse("/briefs", status_code=303)


_FILENAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@app.get("/briefs/{brief_id}/download")
def download_brief(brief_id: int):
    """`.md` як вкладення (розділ 4.8) — без цього єдиний спосіб дістати
    текст бріфа офлайн був би «Копіювати» кнопкою в буфер обміну.
    """
    with db() as conn:
        brief = conn.execute(
            "SELECT title, brief_md FROM kb.briefs WHERE id = %s", (brief_id,)
        ).fetchone()
    if not brief:
        raise HTTPException(404, "brief not found")

    # Ім'я файлу — з title, але санітизоване вайтлістом: заголовок бріфа може
    # містити будь-що (лапки, слеші, кирилицю), а Content-Disposition ламається
    # на символах поза latin-1 і небезпечний на символах на кшталт `"`/`/`.
    stem = _FILENAME_UNSAFE_RE.sub("-", brief["title"] or "").strip("-")[:80]
    filename = f"{stem or f'brief-{brief_id}'}.md"
    return Response(
        content=brief["brief_md"],
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── AI-чат ─────────────────────────────────────────────────────────
#
# Історію пише kbmcp, не admin (розділ 4.9): POST /chat/send нижче лише
# проксує запит на {KBMCP_URL}/chat і читає готову відповідь — той самий
# поділ ролей, що й у generate_brief вище для kb.briefs (пише kbmcp, admin
# показує результат). Тому тут лише SELECT з kb.chat_messages і жодного
# INSERT туди — ОКРІМ /chat/save-brief нижче (розділ B): той пише не в
# kb.chat_messages (це лишається виключно kbmcp), а в kb.briefs — ту саму
# таблицю, яку archive_brief/delete_brief вище вже й так змінюють з admin.


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


# Англійський шаблон питання під форумний чип (розділ D4). Одна коротка фраза,
# англійською навмисно (той самий інваріант, що й _ask_ai_question вище):
# stub-рівень чату відмовляється шукати за не-латинським запитом, тож ask=…
# із чипа має лишатися латиницею незалежно від мови кабінету.
CHAT_FORUM_ASK_EN = "Which dev tooling grants were discussed in {forum} this quarter?"


def _forum_chip_href(display: str) -> str:
    return "/chat?ask=" + quote(CHAT_FORUM_ASK_EN.format(forum=display), safe="")


@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request, error: str = "", ask: str = ""):
    """AI-чат над базою знань (розділ 4.9, задача #30; розділ D — «Ask AI»
    з /items, приклади й форумні чипи на порожньому стані).

    `ask` (розділ D2) лише ПРЕФІЛИТЬ композер на сервері — жодного
    автосабміту: людина сама вирішує, надсилати питання чи спершу
    відредагувати. Обрізання до 4000 — той самий ліміт, що й у POST
    /chat/send (SETTING нижче в шаблоні валідує довжину ще раз на відправці,
    тут це лише страховка від абсурдно довгого query-параметра). Textarea в
    chat.html підставляє значення між тегами — автоескейпінг Jinja вже
    покриває XSS, окремого екранування тут не треба.
    """
    who = auth.session_who(request)
    session_key = f"web:{_chat_key(request)}"
    ask = ask[:4000]

    with db() as conn:
        rows = conn.execute(
            "SELECT id, role, who, content, tier, model, created_at "
            "FROM kb.chat_messages WHERE session_key = %s ORDER BY id LIMIT 200",
            (session_key,),
        ).fetchall()
        # Форумні чипи (розділ D4): лише enabled=true, ORDER BY forum_slug —
        # forum_slug UNIQUE (migrations/004_kb_schema.sql), тож DISTINCT тут
        # не потрібен. Дешево: та сама таблиця, що й на /kb, без JOIN.
        forum_rows = conn.execute(
            "SELECT forum_slug FROM kb.forums WHERE enabled = true ORDER BY forum_slug"
        ).fetchall()

    # Останній рядок — user: відповідь, можливо, ще генерується (POST
    # /chat/send блокується до 300 с — той самий таймаут, що й у /brief) в
    # ІНШІЙ вкладці чи запиті, який саме зараз обробляється. Рядок-натяк, а
    # не спінер із поллінгом — автополінг заборонений (app.js, розділ 0):
    # він тихо продовжував би сесію без участі людини.
    thinking = bool(rows) and rows[-1]["role"] == "user"

    forum_chips = [
        {"label": r["forum_slug"].capitalize(),
         "href": _forum_chip_href(r["forum_slug"].capitalize())}
        for r in forum_rows
    ]

    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "nav": "chat",
            "who": who,
            "messages": rows,
            "thinking": thinking,
            "error": error,
            "ask": ask,
            "forum_chips": forum_chips,
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


@mutations.post("/chat/save-brief")
def save_chat_message_as_brief(request: Request, message_id: int = Form(...)):
    """«Save as brief» (розділ B) на бульбашці асистента LLM-рівня чату.

    На відміну від /chat/send і /chat/new вище, тут ЄДИНИЙ INSERT в усьому
    розділі AI-чату — і не в kb.chat_messages (той запис лишається виключно
    за kbmcp), а в kb.briefs, ту саму таблицю, що archive_brief/delete_brief
    вище вже редагують з admin. Сенс: репліка чату, варта того, щоб її
    показати команді продажів, стає звичайним бріфом — тим самим, що й
    кнопка на /items чи нода n8n — і потрапляє в спільний список /briefs,
    архівується й завантажується тим самим механізмом.

    Guard — рівно два, обидва обов'язкові:
      1. role == 'assistant' і tier == 'llm': кнопка в chat.html рендериться
         лише під такими бульбашками, але сама форма шле голий message_id —
         без цієї перевірки підміна id в DevTools дала б зберегти чиєсь
         питання (role='user') чи keyword-рівня відповідь без жодної LLM-
         синтези за нею.
      2. session_key рядка == сесія ЦЬОГО браузера ('web:' + sid, той самий
         неймспейс, що читає chat_page): без цього підбір послідовних id дав
         би зберегти чужу відповідь — телеграмну чи іншого члена команди.
    Обидва провали віддають однаковий 404 «message not found», а не окремі
    403/400: розрізняти для викликача немає сенсу — жодна з причин не є тим,
    що людина виправляє повторним кліком.
    """
    session_key = f"web:{_chat_key(request)}"

    with db() as conn:
        row = conn.execute(
            "SELECT session_key, role, tier, model, content FROM kb.chat_messages "
            "WHERE id = %s",
            (message_id,),
        ).fetchone()
        if (
            not row
            or row["role"] != "assistant"
            or row["tier"] != "llm"
            or row["session_key"] != session_key
        ):
            raise HTTPException(404, "message not found")

        # Заголовок бріфа — з питання людини, що передувало цій відповіді
        # (той самий session_key, менший id, роль user): «Chat answer» —
        # чесний фолбек для найпершого рядка сесії, де попереднього
        # повідомлення просто не існує.
        preceding = conn.execute(
            "SELECT content FROM kb.chat_messages "
            "WHERE session_key = %s AND id < %s AND role = 'user' "
            "ORDER BY id DESC LIMIT 1",
            (session_key, message_id),
        ).fetchone()
        title = (preceding["content"][:90] if preceding else "") or "Chat answer"

        brief = conn.execute(
            """
            INSERT INTO kb.briefs
                (item_uid, ecosystem, title, brief_md, tier, model, tokens_in, tokens_out)
            VALUES (NULL, 'chat', %s, %s, %s, %s, NULL, NULL)
            RETURNING id
            """,
            (title, row["content"], row["tier"], row["model"]),
        ).fetchone()
        conn.commit()

    return RedirectResponse(f"/briefs/{brief['id']}", status_code=303)


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
