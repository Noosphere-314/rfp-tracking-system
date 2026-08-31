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
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin, urlparse

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

# Вибір моделі для генерації бріфа (задача 4 аудиту 2026-08-11) — той самий
# вайтліст переюзаний у ДВОХ хендлерах (generate_brief і save_chat_report):
# порожнє значення ("Default") ніколи не потрапляє в payload до kbmcp — той
# сам падає назад на settings.brief_model. Список тут, а не в шаблоні:
# шаблони items.html/chat.html рендерять <option> з цього самого джерела
# (templates.env.globals нижче), тож UI і серверна валідація не можуть
# розійтися.
BRIEF_MODEL_CHOICES: list[tuple[str, str, str]] = [
    ("", "f.model.default", "Default"),
    ("claude-sonnet-5", "f.model.sonnet", "Sonnet · faster"),
    ("claude-opus-5", "f.model.opus", "Opus · deeper"),
]
BRIEF_MODEL_ALLOWED = {value for value, _, _ in BRIEF_MODEL_CHOICES if value}

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


# Розділ 3 задачі «read/unread як у месенджерах» (2026-08-11): «потенційно
# цікаве» = ті самі критерії, що вже реалізовані у /items?view=leads24 і
# view=review24 (VIEW_PRESETS нижче) — константи тут, а VIEW_PRESETS
# посилається на ТІ САМІ рядки, а не дублює їх: «переюзай ті самі
# WHERE-шматки, не вигадуй нові» буквально означає одне джерело правди.
# Обидва фрагменти читають лише `i.*` (seen_items), без залежності від
# JOIN sources — тож придатні і для VIEW_PRESETS (де є `s`), і для
# _leads_badge_context нижче (де JOIN на sources не потрібен).
_LEADS24_WHERE = "i.delivered_at > now() - interval '24 hours'"
_REVIEW24_WHERE = (
    "i.category = 'FUNDING' AND i.delivered_at IS NULL "
    "AND i.first_seen > now() - interval '24 hours'"
)


def _leads_badge_context(request: Request) -> dict:
    """NAV-бейдж (розділ B2 — «ліди видимі в дашборді», розділ 3 — «read/
    unread»): один комбінований запит рахується тут, а не в кожному
    хендлері окремо, бо NAV (base.html) рендериться на КОЖНІЙ сторінці, а
    не лише на /items чи /. Той самий механізм, що й auth.template_context
    і i18n.template_context нижче — context_processors викликає
    Jinja2Templates рівно раз на кожен рендер шаблону, тобто рівно один
    SELECT на одну відповідь (а не п'ять разів на сторінку).

    Два числа, а не одне (задача 3 аудиту 2026-08-11):
      leads_24h     — ліди за останні 24 год (як і раніше; плитка Overview
                      «New leads (24h)» лишається незмінною).
      unread_count  — НЕПРОЧИТАНІ (viewed_at IS NULL) серед «потенційно
                      цікавого» (leads24 ∪ review24) — це і є нове число на
                      NAV-бейджі біля «Знахідки» і на плитці Overview
                      «Unread findings».
    Обидва — FILTER-агрегати ОДНОГО запиту (той самий прийом, що й
    source_health у dashboard() нижче), а не два execute(): один SELECT
    замість двох тримає вартість на кожній сторінці однаковою, що й до
    цієї зміни.

    Свідома вартість: /login (публічний, неавторизований) теж проходить
    крізь цей самий Jinja2Templates env, тож і туди прилетить один зайвий
    SELECT — прийнятно, бо логін відвідують рідко, а не на гарячому шляху.
    ЧЕСНО: seen_items.delivered_at СЬОГОДНІ без окремого індексу
    (migrations/001_core_schema.sql — є лише (source_id, first_seen DESC));
    COUNT тут — послідовне сканування, не «дешевий індексований підрахунок».
    Таблиця поки невелика, тож це прийнятно як старт, але міграція з
    частковим індексом `WHERE delivered_at IS NOT NULL` — природний
    наступний крок (поза межами цієї задачі: міграції належать паралельному
    агенту).

    Помилка БД тут НЕ має валити сторінку — бейдж лише підказка, а не
    критичний шлях (той самий компроміс, що й /healthz).
    """
    try:
        with db() as conn:
            row = conn.execute(
                f"""
                SELECT
                    count(*) FILTER (WHERE {_LEADS24_WHERE}) AS leads_24h,
                    count(*) FILTER (
                        WHERE i.viewed_at IS NULL
                          AND (({_LEADS24_WHERE}) OR ({_REVIEW24_WHERE}))
                    ) AS unread_count
                  FROM seen_items i
                """
            ).fetchone()
        leads_24h = row["leads_24h"] if row else 0
        unread_count = row["unread_count"] if row else 0
    except Exception:  # noqa: BLE001 — бейдж не критичний, сторінка не має падати
        leads_24h = 0
        unread_count = 0
    return {"leads_24h": leads_24h, "unread_count": unread_count}


# Executive «спрощений вигляд» (запит Миколи 2026-08-31). Спільний пароль на
# весь дашборд означає відсутність справжніх ролей (auth.py, розділ «Хто саме
# входить») — тому цей cookie НЕ входить у підписану сесію, як і rfp_lang в
# admin/i18n.py: вибір «простий/повний» вигляд сайдбару — преференція
# перегляду, а не автентифікація, і перевидавати підписаний сесійний cookie
# заради косметики не варто (той самий аргумент, що й у i18n.py docstring).
VIEW_COOKIE = "rfp_view"


def _view_context(request: Request) -> dict:
    """Той самий механізм, що й _leads_badge_context/auth.template_context/
    i18n.template_context вище: сайдбар (base.html) рендериться на КОЖНІЙ
    сторінці, тож «хто дивиться» і «який вигляд обрав» рахуються раз тут, а
    не в кожному хендлері.

    simple_view: щойно Executive заходить у кабінет — сайдбар показує лише
    групу Work (Огляд/Знахідки/Бріфи/База знань/AI-чат), без Налаштувань,
    Системи й зовнішнього n8n. VIEW_COOKIE == "full" — явний вихід із цього
    режиму кнопкою «Full version» (маршрут POST /view нижче); будь-яке інше
    значення (відсутнє, стерте, сміття) означає «спрощений».

    show_view_toggle: кнопка-перемикач в base.html видима ЛИШЕ Executive —
    для решти підрозділів нав і так повний, перемикати нічого.

    БЕЗПЕКА: це презентація, не авторизація. Приховані пункти меню — не
    заборонені сторінки: прямий URL (наприклад /sources) відкривається
    достоту так само, як і людині з повним сайдбаром, — спільний пароль не
    дає системі справжніх ролей, аби на щось таке спиратися.
    """
    who = auth.session_who(request)
    is_exec = who == "Executive"
    return {
        "simple_view": is_exec and request.cookies.get(VIEW_COOKIE) != "full",
        "show_view_toggle": is_exec,
    }


templates = Jinja2Templates(
    directory=Path(__file__).parent / "templates",
    context_processors=[
        auth.template_context, i18n.template_context, _leads_badge_context, _view_context,
    ],
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
# Задача 5 аудиту 2026-08-12: «briefs» переїхав із "sys" у "work" — бріфи це
# щоденна робоча сторінка (їх відкривають продажі), а не системна
# діагностика, тож і місце їй поруч зі «Знахідками», не з «Історією збору».
# Порядок у Work — навмисний: Overview → Findings → Briefs → Knowledge base →
# AI chat, той самий шлях, яким лід проходить конвеєр (від огляду стану до
# джерела знань). System тепер лишає рівно один пункт — «Історія збору»
# (діагностика воркера, не щоденна робота).
NAV = [
    {"id": "dashboard", "href": "/", "label": "Огляд", "group": "work"},
    {"id": "items", "href": "/items", "label": "Знахідки", "group": "work"},
    {"id": "briefs", "href": "/briefs", "label": "Бріфи", "group": "work"},
    {"id": "kb", "href": "/kb", "label": "База знань", "group": "work"},
    {"id": "chat", "href": "/chat", "label": "AI-чат", "group": "work"},
    {"id": "sources", "href": "/sources", "label": "Джерела", "group": "cfg"},
    {"id": "keywords", "href": "/keywords", "label": "Ключові слова", "group": "cfg"},
    {"id": "settings", "href": "/settings", "label": "Параметри", "group": "cfg"},
    {"id": "runs", "href": "/runs", "label": "Історія збору", "group": "sys"},
    # «Історія чатів» свідомо БЕЗ пункту меню: вона живе постійною панеллю
    # праворуч усередині AI-чату (/chat?hist=…, рішення Миколи 2026-08-11);
    # маршрути /chats* лишаються як повноекранний вигляд за прямим лінком
    # «Open full» із панелі.
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


def compact_num(n: int | None) -> str:
    """Компактний формат великих чисел для таблиці /kb (задача «покриття
    архівів»): 1234 → «1.2k», 1_500_000 → «1.5M». Під тисячею — число як є,
    без десяткової частини. `None` сюди не приходить — NULL обробляється
    в шаблоні окремою гілкою (потрібен інший текст «—», а не «0»)."""
    if n < 1000:
        return str(n)
    value = n / 1000 if n < 1_000_000 else n / 1_000_000
    suffix = "k" if n < 1_000_000 else "M"
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{text}{suffix}"


def _bar_pct(value: int, max_value: int) -> int:
    """→ бакетований відсоток (крок 5, 0..100) для CSS-класу `.w-N` на
    Overview (задача 3 аудиту 2026-08-12: CSS-бар-чарти «Activity» і «Top
    ecosystems»). CSP тут principled — той самий інваріант, що і скрізь у
    файлі (жодного style=): ширина бару приходить КЛАСОМ, а не інлайн-стилем,
    тож app.css мусить мати статичний набір `.w-0`..`.w-100` кроком 5, а
    Python лише обирає, який із них підставити в шаблон.

    `max_value <= 0` (порожній 14-денний зріз чи 0 знахідок за 7 днів) →
    0 замість ділення на нуль — порожній бар, а не крах сторінки."""
    if max_value <= 0:
        return 0
    pct = round(value / max_value * 100 / 5) * 5
    return max(0, min(100, int(pct)))


templates.env.filters["hl"] = hl
templates.env.filters["linkify"] = linkify
templates.env.filters["md_lite"] = md_lite
templates.env.filters["compact"] = compact_num
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
    BRIEF_MODEL_CHOICES=BRIEF_MODEL_CHOICES,
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


_BRIEF_MAGIC_RE = re.compile(r"/briefs/(\d+)")


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
        # Magic-лінк на КАНОНІЧНОМУ шляху бріфа: перше TG-повідомлення
        # 2026-08-31 пішло з /briefs/{id}?t=… (воркфлоу оновили до /share/
        # пізніше), і такі лінки живуть у групі тижнями — без сесії їх
        # веде на share-вигляд, а НЕ на логін-стіну. Лише редірект:
        # валідність токена перевіряє сам share_brief_page (404 на кривий).
        # Залогінені сюди не потрапляють — вони бачать повний /briefs/{id}.
        m = _BRIEF_MAGIC_RE.fullmatch(path)
        token = request.query_params.get("t", "")
        if m and token:
            return RedirectResponse(
                f"/share/briefs/{m.group(1)}?{urlencode({'t': token})}",
                status_code=303,
            )
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


@app.post("/view", dependencies=[Depends(auth.csrf_guard)])
def switch_view(request: Request, view: str = Form(""), next: str = Form("/")):
    """Перемикач «Simple view» ⇄ «Full version» у сайдбарі Executive — той
    самий трюк, що й /lang вище: окрема НЕ сесійна cookie (VIEW_COOKIE), бо
    це преференція перегляду, а не авторизація (див. _view_context).

    Кнопка в base.html навмисно ОДНА: submit-кнопка несе `value="full"`, коли
    зараз показано спрощений сайдбар, і порожній рядок, коли зараз показано
    повний, — тобто саме той стан, У ЯКИЙ людина хоче перейти. Будь-яке
    значення, відмінне від "full" (порожнє, сміття від чужого клієнта),
    трактується як «стерти cookie» — cookie й так лише презентаційна, гіршого
    за «сайдбар знову спрощений» тут статися не може.
    """
    response = RedirectResponse(auth.safe_next(next), status_code=303)
    if view == "full":
        response.set_cookie(
            VIEW_COOKIE,
            "full",
            max_age=365 * 24 * 3600,
            path="/",
            httponly=False,
            secure=request.url.scheme == "https",
            samesite="lax",
        )
    else:
        response.delete_cookie(VIEW_COOKIE, path="/")
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
#
# Переосмислення (задача 6 аудиту 2026-08-11, запит Миколи «не розумію, як
# це читати»): мета сторінки — сейлз відкриває і за 10 секунд розуміє «що
# нового / чи все працює / як тиждень». П'ять дешевих агрегатних запитів
# замість колишніх N+1-схильних сирих таблиць («Source health» на 12 рядків,
# «Queued and stuck» на 20) — їхню роль тепер грає «Needs attention»: лише
# проблеми, кожна — рядок із лінком на сторінку дії. Порожньо — зелений
# порожній стан, а не порожня таблиця.

# «Проблема» для панелі Needs attention — сталий список (kind, href) у
# порядку показу; текст і лічильник рахуються нижче в dashboard(). Порядок
# тут навмисний: спершу джерела (найчастіша причина тиші), далі збори,
# черга, і насамкінець KB — та сама послідовність, що й у воронці «звідки
# ліди беруться».
_ATTENTION_KINDS = (
    ("quarantined_sources", "pg.overview.issue.quarantined",
     "%(n)s source(s) in quarantine", "/sources"),
    ("fetch_failures_24h", "pg.overview.issue.fetch_failures",
     "%(n)s source fetch failure(s) in the last 24 hours", "/runs"),
    ("pending_stuck", "pg.overview.issue.pending_stuck",
     "%(n)s finding(s) stuck pending for over 2 hours", "/items?status=pending"),
    ("stale_kb_forums", "pg.overview.issue.stale_kb",
     "%(n)s knowledge-base forum(s) with no new activity in 30+ days", "/kb"),
)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with db() as conn:
        # Плитки-дії (розділ 1): leads_24h/unread_count приходять із того
        # самого context-процесора, що й NAV-бейдж (_leads_badge_context) —
        # друга й перша плитки не додають жодного SELECT тут. Ці дві —
        # операційні (24 год), рахуються підзапитами по НЕзалежних таблицях
        # в ОДНОМУ execute(), а не окремими походами в БД.
        tiles = conn.execute(
            """
            SELECT
                (SELECT coalesce(sum(items_seen), 0) FROM worker_runs
                  WHERE started_at > now() - interval '24 hours') AS collected_24h,
                (SELECT count(*) FROM kb.briefs
                  WHERE created_at > now() - interval '7 days') AS briefs_7d
            """
        ).fetchone()

        # Панель «Needs attention» (розділ 2): чотири незалежні підрахунки в
        # ОДНОМУ запиті — жодного N+1, і жодна з таблиць тут не велика
        # (sources/worker_runs/seen_items(pending)/kb.forums), тож ціна
        # прийнятна для сторінки, яку відкривають на кожному заході.
        attention = conn.execute(
            """
            SELECT
                (SELECT count(*) FROM sources WHERE quarantined) AS quarantined_sources,
                (SELECT coalesce(sum(sources_failed), 0) FROM worker_runs
                  WHERE started_at > now() - interval '24 hours') AS fetch_failures_24h,
                (SELECT count(*) FROM seen_items
                  WHERE status = 'pending'
                    AND first_seen < now() - interval '2 hours') AS pending_stuck,
                (SELECT count(*) FROM (
                    SELECT f.id
                      FROM kb.forums f
                      LEFT JOIN kb.topics t ON t.forum_slug = f.forum_slug
                     WHERE f.enabled
                     GROUP BY f.id
                    HAVING max(t.bumped_at) IS NULL
                        OR max(t.bumped_at) < now() - interval '30 days'
                 ) stale) AS stale_kb_forums
            """
        ).fetchone()

        # Топ-3 проблемних джерела (розділ 4 — «максимум табличне всередині
        # Needs attention»): та сама сортировка, що й колишня повна таблиця
        # source_health, лише LIMIT 3 й лише реально проблемні рядки.
        top_problem_sources = conn.execute(
            """
            SELECT name, ecosystem, consecutive_failures, quarantined
              FROM sources
             WHERE quarantined OR consecutive_failures > 0
             ORDER BY consecutive_failures DESC, last_item_at ASC NULLS FIRST
             LIMIT 3
            """
        ).fetchall()

        # Панель «This week» (розділ 3): міні-воронка одним запитом
        # (FILTER-агрегати, той самий прийом, що й вище) — усі чотири числа
        # рахують той самий базовий зріз (first_seen > 7 днів), що й лінк
        # /items?period=7d нижче показав би, тож число на плитці завжди
        # збігається з тим, що людина побачить після кліку.
        funnel = conn.execute(
            """
            SELECT
                count(*) AS collected,
                count(*) FILTER (WHERE status <> 'filtered') AS passed_filter,
                count(*) FILTER (WHERE delivered_at IS NOT NULL) AS leads,
                count(*) FILTER (WHERE outcome IN ('won', 'lost')) AS closed
              FROM seen_items
             WHERE first_seen > now() - interval '7 days'
            """
        ).fetchone()

        # Чесний бейдж режиму (розділ 4.1, F6): поки класифікатор — заглушка з
        # захардкодженим confidence 0.55, будь-яка аналітика по впевненості
        # малювала б сотню однакових барів і виглядала б робочою.
        last_verdict = conn.execute(
            "SELECT prompt_version FROM items_log WHERE event = 'classified' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

        # ── Задача 3 аудиту 2026-08-12: віджети даних під «This week» ──────
        #
        # «Activity, last 14 days» — один запит: generate_series дає РІВНО 14
        # календарних днів (навіть ті, де воркер нічого не приніс), LEFT JOIN
        # підтягує агрегати з seen_items — тож порожні дні лишаються рядками з
        # нулями, а не зникають (графік не «стискається» на тижні без збору).
        activity_rows = conn.execute(
            """
            WITH days AS (
                SELECT generate_series(
                    (now() - interval '13 days')::date, now()::date, interval '1 day'
                )::date AS day
            ), agg AS (
                SELECT first_seen::date AS day,
                       count(*) AS collected,
                       count(*) FILTER (WHERE delivered_at IS NOT NULL) AS leads
                  FROM seen_items
                 WHERE first_seen > now() - interval '14 days'
                 GROUP BY first_seen::date
            )
            SELECT d.day, coalesce(a.collected, 0) AS collected,
                   coalesce(a.leads, 0) AS leads
              FROM days d LEFT JOIN agg a ON a.day = d.day
             ORDER BY d.day
            """
        ).fetchall()

        # «Top ecosystems (7d)» — топ-5 за кількістю знахідок за 7 днів.
        top_ecosystems = conn.execute(
            """
            SELECT s.ecosystem, count(*) AS n
              FROM seen_items i
              JOIN sources s ON s.id = i.source_id
             WHERE i.first_seen > now() - interval '7 days'
             GROUP BY s.ecosystem
             ORDER BY n DESC
             LIMIT 5
            """
        ).fetchall()

        # «Latest leads» — 5 останніх лідів, з тим самим LEFT JOIN LATERAL на
        # kb.briefs, що й /items (задача 2 аудиту 2026-08-11): якщо бріф уже
        # існує, рядок отримує пряме посилання «Open brief» замість форми
        # створення (та форма на Overview свідомо не дублюється, п.3 задачі 3
        # аудиту 2026-08-12 — «можна спростити»).
        latest_leads = conn.execute(
            """
            SELECT i.item_uid, i.title, i.url, i.delivered_at,
                   s.ecosystem AS source_ecosystem, bf.id AS brief_id
              FROM seen_items i
              JOIN sources s ON s.id = i.source_id
              LEFT JOIN LATERAL (
                  SELECT b.id FROM kb.briefs b WHERE b.item_uid = i.item_uid
                   ORDER BY b.id DESC LIMIT 1
              ) AS bf ON true
             WHERE i.delivered_at IS NOT NULL
             ORDER BY i.delivered_at DESC
             LIMIT 5
            """
        ).fetchall()

    # Бакетовані ширини барів (app.css: .w-0..w-100) рахуються тут, а не в
    # шаблоні (той самий принцип, що й issues вище): кожен ряд масштабується
    # відносно МАКСИМУМУ у своєму власному 14-денному ряду — інакше один
    # рідкісний сплеск лідів робив би решту днів невидимо тонкими смужками.
    # `or 1` — не ділити на нуль, коли весь період порожній (усі бари w-0).
        # «Closing soon» (дедлайн-трекер, план 2026-08-31 п.2): відкриті
        # дедлайни в найближчі 14 днів. Джерело рядків — щотижневий
        # grants-звіт (kb.deadlines, міграція 015); dismissed приховані
        # НАЗАВЖДИ — «прибрав з очей» це рішення людини, повторна згадка
        # у звіті його не скасовує (upsert не чіпає dismissed_at).
        closing_deadlines = conn.execute(
            """
            SELECT id, title, ecosystem, deadline, url,
                   (deadline - current_date) AS days_left
              FROM kb.deadlines
             WHERE dismissed_at IS NULL
               AND deadline BETWEEN current_date AND current_date + 14
             ORDER BY deadline, id LIMIT 8
            """
        ).fetchall()

    max_collected = max((r["collected"] for r in activity_rows), default=0) or 1
    max_leads = max((r["leads"] for r in activity_rows), default=0) or 1
    activity = [
        {
            "day": r["day"], "collected": r["collected"], "leads": r["leads"],
            "collected_pct": _bar_pct(r["collected"], max_collected),
            "leads_pct": _bar_pct(r["leads"], max_leads),
        }
        for r in activity_rows
    ]
    max_eco = max((r["n"] for r in top_ecosystems), default=0) or 1
    ecosystems = [
        {"ecosystem": r["ecosystem"], "n": r["n"], "pct": _bar_pct(r["n"], max_eco)}
        for r in top_ecosystems
    ]
    # Порожній стан «Activity» — коли ЖОДЕН із 14 днів не приніс жодної
    # знахідки (не лише «max_collected==1 з дефолту»): `any(...)` читає
    # сирі рядки, а не пост-бакетовані pct, тож 1 знахідка за 14 днів усе
    # одно рендерить графік (з майже порожніми барами), а НУЛЬ — порожній стан.
    activity_has_data = any(r["collected"] for r in activity_rows)

    # Текст і число кожної проблеми — тут, не в шаблоні (той самий принцип,
    # що й _ask_ai_question вище): шаблон лише перекладає готовий
    # (ключ, %(n)s-рядок) через t() і рендерить лінк. Нуль проблем → шаблон
    # сам показує зелений порожній стан.
    issues = [
        {"key": key, "default": default, "n": attention[field], "href": href}
        for field, key, default, href in _ATTENTION_KINDS
        if attention[field]
    ]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "nav": "dashboard",
            "collected_24h": tiles["collected_24h"],
            "briefs_7d": tiles["briefs_7d"],
            "issues": issues,
            "top_problem_sources": top_problem_sources,
            "funnel": funnel,
            "activity": activity,
            "activity_has_data": activity_has_data,
            "ecosystems": ecosystems,
            "latest_leads": latest_leads,
            "closing_deadlines": closing_deadlines,
            "stub_classifier": bool(
                last_verdict and last_verdict["prompt_version"] == "stub-no-llm"
            ),
        },
    )


# ── Sources ────────────────────────────────────────────────────────


SOURCE_FORM_FIELDS = ("type", "name", "ecosystem", "url", "category", "lane", "config")

# Дефолт-шаблони Config за типом фетчера (задача «менше ручного JSON», п.2):
# app.js підставляє потрібний у порожню textarea при зміні select-а типу,
# підказка під полем показує всі варіанти для тих, у кого немає JS. Типи без
# config (rss, defillama) — жоден worker/fetchers/*.py файл не чіпає
# source.config.get(...) для них, порожній об'єкт уже валідний.
CONFIG_TEMPLATES: dict[str, str] = {
    "discourse": '{"categories": []}',
    # ФОРМА, яку реально читає worker/fetchers/github_discussions.py:
    # repo.get("owner") / repo.get("name"), тобто список ОБ'ЄКТІВ, а не
    # рядків "owner/repo" (той варіант фетчер відкидає як "malformed repo
    # entry"). Шаблон мусить збігатися з фетчером, інакше форма підказує
    # конфіг, який гарантовано не працює.
    "github_discussions": '{"repos": [{"owner": "owner", "name": "repo"}]}',
    "snapshot": '{"spaces": ["example.eth"]}',
    "rest_aggregator": '{"items_path": "data", "fields": {}}',
    "rss": "{}",
    "defillama": "{}",
}

# cats[] приходить з чекбоксів "Discover" як "<id>:<slug>" (розділ нижче) —
# регулярка відсікає все, що не схоже на пару discourse id:slug, перш ніж
# значення потрапить у JSON, що піде в БД.
CATS_RE = re.compile(r"^\d+:[a-z0-9-]{1,80}$")
MAX_DISCOVERED_CATS = 40


def _render_sources(
    request: Request,
    *,
    message: str = "",
    error: str = "",
    form: dict | None = None,
    discovered: list[dict] | None = None,
    status_code: int = 200,
):
    """Одна точка рендеру /sources — щоб помилкова гілка `add_source` могла
    повернути сторінку з уже введеними значеннями (розділ 4.2), а не редірект,
    після якого 7 полів і JSON-конфіг доводиться набирати заново.

    `discovered` — результат "Discover" для discourse (нижче): список
    знайдених категорій, який форма показує як чекбокси замість того, щоб
    примушувати вручну писати JSON."""
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
            "config_templates": CONFIG_TEMPLATES,
            "discovered_categories": discovered or [],
        },
        status_code=status_code,
    )


@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, message: str = "", error: str = "", url: str = ""):
    """`url` — префіл форми додавання (план 2026-08-31, функц. п.4): лінки
    «Add: /sources?url=…» з discovery-звіту ведуть одразу на заповнене поле —
    людині лишається Detect type → Discover → Test and save. Обрізання і
    м'яка перевірка схеми — захист від сміття в query, не від людини:
    невалідне значення просто не префілиться."""
    url = url.strip()[:500]
    if url and not url.startswith(("http://", "https://")):
        url = ""
    return _render_sources(request, message=message, error=error,
                           form={"url": url} if url else None)


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


def _flatten_discourse_categories(categories: list[dict], depth: int = 0) -> list[dict]:
    """`categories.json?include_subcategories=true` вкладає підкатегорії в
    `subcategory_list` кожного батька — тут це розгортається в плаский список
    для чекбоксів форми, з `depth` для відступу в розмітці. Кожен рівень
    (батьки між собою, діти під своїм батьком між собою) — спаданням за
    topic_count, як просив власник: спершу найактивніші."""
    out: list[dict] = []
    for cat in sorted(categories, key=lambda c: c.get("topic_count") or 0, reverse=True):
        cat_id, slug = cat.get("id"), cat.get("slug")
        if cat_id is None or not slug:
            continue
        out.append({
            "id": cat_id, "slug": slug,
            "topic_count": cat.get("topic_count") or 0, "depth": depth,
        })
        subs = cat.get("subcategory_list") or []
        if subs:
            out.extend(_flatten_discourse_categories(subs, depth + 1))
    return out


def _discover_discourse(url: str) -> tuple[list[dict], str]:
    """Живий, нічого не зберігаючий похід на `{url}/categories.json` — той
    самий патерн read-only з'єднання, що й _test_fetch вище (netguard
    всередині HttpClient/_get_checked, окремого обходу тут немає).
    Повертає (категорії, помилка) — рівно як _test_fetch повертає (count, error).

    Задача 2 (2026-08-12): власник спробував https://ethereum.forum/ — це
    SPA, що на БУДЬ-ЯКИЙ /*.json віддає HTML зі статусом 200, тож `.json()`
    падав із сирим `JSONDecodeError` просто в повідомленні форми. Тепер
    тіло, що не парситься як JSON, або парситься, але не має форми Discourse
    (ні `category_list`, ні `about`) — один людський рядок з підказкою
    натиснути "Detect type"; технічна деталь (перші 80 символів тіла) іде в
    дужках наприкінці, для діагностики, а не як основне повідомлення."""
    endpoint = f"{url.rstrip('/')}/categories.json?include_subcategories=true"
    host = urlparse(url).netloc or url
    with db() as conn, HttpClient(conn) as client:
        try:
            response = client.get(endpoint, use_cache=False)
        except SourceBlocked as exc:
            return [], f"blocked (403/429): {exc}"
        except Exception as exc:  # noqa: BLE001 — мережа/SSRF — усе веде до помилки форми, не 500
            return [], f"{type(exc).__name__}: {exc}"

        try:
            payload = response.json()
            if not isinstance(payload, dict) or not (
                isinstance(payload.get("category_list"), dict)
                or isinstance(payload.get("about"), dict)
            ):
                raise ValueError("unexpected JSON shape")
        except Exception as exc:  # noqa: BLE001 — не-JSON або не-Discourse форма відповіді (ethereum.forum)
            detail = response.text[:80].strip() if response.text else type(exc).__name__
            return [], (
                f'{host} did not answer with Discourse JSON — it is probably not a '
                f'Discourse forum. Press "Detect type" to check what it is. ({detail})'
            )

    categories = ((payload.get("category_list") or {}).get("categories")) or []
    if not categories:
        return [], "No categories found in the response — is this a Discourse forum?"
    return _flatten_discourse_categories(categories), ""


# ── Detect type ────────────────────────────────────────────────────────────
#
# Продовження задачі вище: власник хоче кнопку, яка САМА визначає тип форуму
# з самого лише URL, замість того, щоб гадати й отримувати JSONDecodeError.
# Регекс замість повного HTML-парсера — шукаємо рівно один патерн тегу
# <link rel="alternate" type="application/…+xml" href="…">, а не довільну
# розмітку (задача 1, п.4).
_LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_REL_ALTERNATE_RE = re.compile(r"""rel=["']alternate["']""", re.IGNORECASE)
_FEED_TYPE_RE = re.compile(r"""type=["']application/(?:rss|atom)\+xml["']""", re.IGNORECASE)
_HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.IGNORECASE)
DETECT_HTML_READ_LIMIT = 100_000  # ~100 КБ — досить для <head>, без завантаження цілої сторінки в пам'ять


def _find_feed_link(html: str) -> str | None:
    """Перший `<link rel="alternate" type="application/rss+xml|atom+xml" href="…">`
    у HTML — href може бути відносним, абсолютизує викликач (urljoin на origin)."""
    for tag in _LINK_TAG_RE.findall(html):
        if _REL_ALTERNATE_RE.search(tag) and _FEED_TYPE_RE.search(tag):
            href = _HREF_RE.search(tag)
            if href:
                return href.group(1)
    return None


def _detect_source_type(url: str) -> tuple[str, str, str]:
    """"Detect type" (задача 1, 2026-08-12) — третя кнопка форми /sources,
    видима завжди (на відміну від Discover, лише для discourse): власник
    вводить сам лише URL і хоче знати, ЯКИЙ це тип джерела, перш ніж писати
    Config JSON вручну.

    Перевірки йдуть по порядку від дешевих (лише хост, без мережі) до
    мережевих (через HttpClient — той самий netguard/SSRF-захист, що й
    _discover_discourse/_test_fetch вище):
      1. snapshot.org у хості → snapshot
      2. github.com/<owner>/<repo> → github_discussions
      3. {origin}/about.json з РЕАЛЬНИМ about.stats → discourse (не просто
         200 OK — SPA-хости на кшталт ethereum.forum віддають HTML-заглушку
         зі статусом 200 на будь-який шлях)
      4. <link rel="alternate" type="application/…+xml"> на кореневій
         сторінці → rss
      5. інакше — людська помилка з назвою хоста

    Повертає (type, hint, error): рівно одне з (type і hint непорожні) або
    (error непорожній) — ніколи виняток назовні, мережа/парсинг завжди
    зводяться до людського тексту."""
    raw = url.strip()
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    host = parsed.netloc.lower()
    if not host:
        return "", "", f"«{url}» doesn't look like a URL"
    origin = f"{parsed.scheme}://{parsed.netloc}"

    # 1. Snapshot — простір голосувань живе під одним хостом, мережа не потрібна.
    if "snapshot.org" in host:
        return (
            "snapshot",
            'Snapshot space list goes in config: {"spaces": ["name.eth"]}',
            "",
        )

    # 2. GitHub Discussions — owner/repo видно прямо в шляху URL.
    if host == "github.com":
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) < 2:
            return (
                "", "",
                "github.com URL must include owner and repo, "
                "e.g. https://github.com/owner/repo",
            )
        owner, repo = parts[0], parts[1]
        return (
            "github_discussions",
            # Той самий формат, що й CONFIG_TEMPLATES вище і що читає фетчер.
            f'GitHub repo detected — use config: '
            f'{{"repos": [{{"owner": "{owner}", "name": "{repo}"}}]}}',
            "",
        )

    # 3. Discourse — {origin}/about.json з about.stats. Мовчазний except:
    # будь-яка мережева/SSRF/не-JSON проблема тут просто означає "не Discourse",
    # переходимо до перевірки RSS нижче, а не показуємо помилку одразу.
    with db() as conn, HttpClient(conn) as client:
        try:
            payload = client.get(f"{origin}/about.json", use_cache=False).json()
        except Exception:  # noqa: BLE001
            payload = None

    about_stats = None
    if isinstance(payload, dict):
        about_field = payload.get("about")
        if isinstance(about_field, dict) and isinstance(about_field.get("stats"), dict):
            about_stats = about_field["stats"]

    if about_stats is not None:
        # topicS_count / postS_count — саме так називає їх Discourse у
        # /about.json (перевірено наживо на ethereum-magicians.org
        # 2026-08-12: без «s» повідомлення показувало «? topics / ? posts»).
        # Однина лишається запасним варіантом на випадок іншої версії.
        topics = about_stats.get("topics_count") or about_stats.get("topic_count", "?")
        posts = about_stats.get("posts_count") or about_stats.get("post_count", "?")
        return (
            "discourse",
            f"Discourse forum: {topics} topics / {posts} posts — press Discover to pick categories",
            "",
        )

    # 4. RSS/Atom — <link rel="alternate" …> на кореневій сторінці.
    with db() as conn, HttpClient(conn) as client:
        try:
            html = client.get(origin, use_cache=False).text[:DETECT_HTML_READ_LIMIT]
        except Exception:  # noqa: BLE001 — немає й RSS — впадемо в гілку 5 нижче
            html = ""

    href = _find_feed_link(html)
    if href:
        absolute = urljoin(origin, href)
        return "rss", f"RSS feed found: {absolute} — use that URL as the source URL", ""

    # 5. Нічого не підійшло.
    return (
        "", "",
        f"No public API or feed found at {host}. This site is likely a JavaScript app "
        "without an open API — it cannot be tracked. Try the project's Discourse forum "
        "(…/about.json responds with JSON) or an RSS feed.",
    )


def _cats_to_config(cats: list[str]) -> dict:
    """"<id>:<slug>" з чекбоксів Discover → {"categories": [{"slug","id"}, …]}
    у форматі, який worker/fetchers/discourse.py читає (source.config["categories"]).
    Невалідні значення (не пройшли CATS_RE) мовчки відкидаються, решта — не
    більше MAX_DISCOVERED_CATS."""
    selected: list[dict] = []
    for entry in cats:
        if not CATS_RE.match(entry):
            continue
        cat_id, slug = entry.split(":", 1)
        selected.append({"slug": slug, "id": int(cat_id)})
        if len(selected) >= MAX_DISCOVERED_CATS:
            break
    return {"categories": selected} if selected else {}


@mutations.post("/sources/add")
def add_source(
    request: Request,
    action: str = Form("save"),
    type: str = Form(...),
    name: str = Form(""),
    ecosystem: str = Form(""),
    url: str = Form(""),
    category: str = Form(""),
    lane: str = Form("rfp"),
    config: str = Form("{}"),
    cats: list[str] = Form(default=[]),
):
    """Додавання з живим тест-фетчем — та сама гарантія, що дає n8n-форма:
    джерело, яке зараз не може віддати жодного елемента, не зберігається
    увімкненим.

    Помилкові гілки повертають ВІДРЕНДЕРЕНУ сторінку зі збереженими полями
    (розділ 4.2), а не редірект: тест-фетч триває до 30 с, і втрачати після
    нього сім полів разом із JSON-конфігом — найдорожча дрібниця цієї сторінки.

    `action=discover` (друга кнопка форми, лише для discourse) — окрема гілка
    ПЕРЕД валідацією name/ecosystem/url required: власник хоче спершу
    побачити список категорій форуму, а тоді вже дописувати решту полів.

    `action=detect` (перша кнопка, "Detect type", задача 1 2026-08-12) —
    ще одна гілка перед тією ж валідацією: потребує лише URL, викликає
    `_detect_source_type` і при успіху перемикає `type` у формі, що
    рендериться нижче, на визначений."""
    submitted = {
        "type": type, "name": name, "ecosystem": ecosystem, "url": url,
        "category": category, "lane": lane, "config": config,
    }

    if action == "detect":
        if not url.strip():
            return _render_sources(
                request, error="URL is required to detect the source type",
                form=submitted, status_code=400,
            )
        detected_type, hint, detect_error = _detect_source_type(url.strip())
        if detect_error:
            return _render_sources(
                request, error=detect_error,
                form=submitted, status_code=400,
            )
        submitted["type"] = detected_type
        return _render_sources(
            request, message=hint,
            form=submitted, status_code=200,
        )

    if action == "discover":
        if type != "discourse":
            return _render_sources(
                request,
                error="Discover works for discourse sources only",
                form=submitted, status_code=400,
            )
        if not url.strip():
            return _render_sources(
                request, error="URL is required to discover categories",
                form=submitted, status_code=400,
            )
        found, disc_error = _discover_discourse(url.strip())
        if disc_error:
            return _render_sources(
                request, error=f"Discover failed: {disc_error}",
                form=submitted, status_code=400,
            )
        return _render_sources(
            request,
            message=f"Found {len(found)} categories — tick the ones to track, then Test and save",
            form=submitted, discovered=found, status_code=200,
        )

    if not name.strip() or not ecosystem.strip() or not url.strip():
        return _render_sources(
            request, error="Name, ecosystem and URL are required",
            form=submitted, status_code=400,
        )
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

    # Ручний config має пріоритет — але «ручним» вважається лише той, що
    # НЕСЕ ДАНІ. Знайдено на живому додаванні gov.uniswap.org 2026-08-12:
    # app.js підставляє в порожню textarea шаблон типу ({"categories": []}),
    # і перевірка `not config_obj` бачила непорожній dict → позначені
    # чекбокси Discover мовчки ігнорувались, а фетчер падав сирим
    # «needs config.categories». Порожній шаблон = те саме, що порожнє поле.
    if cats and not any(config_obj.values()):
        config_obj = _cats_to_config(cats)

    # Discourse без жодної категорії далі впаде всередині фетчера технічним
    # ValueError — ловимо ДО походу в мережу і кажемо, що саме зробити.
    if type == "discourse" and not (config_obj.get("categories") or []):
        return _render_sources(
            request,
            error=(
                "Discourse sources need at least one category: press "
                "\"Discover categories\" above and tick what to track."
            ),
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
    "weekly_forum_denylist": {
        "group": "ai", "label": "Weekly report: never suggest these forums", "type": "text",
        "help": "Comma-separated names the Monday discovery report must never propose "
                "in its \"Forums worth adding\" section. A forum that was deliberately "
                "dropped is absent from the tracked list for that very reason, so "
                "without this the report keeps re-suggesting it every week.",
        "reco": "'lido' — removed from the system on purpose in August 2026. Add any "
                "other ecosystem the team has decided against.",
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
    # Розділ 7 редизайну чату (рішення Миколи 2026-08-11) — два нові ключі,
    # обидва читає kbmcp паралельно з цим PR: model routing на дешевшу
    # модель для навігаційних питань і TTL кешу відповіді на перше питання
    # розмови. Текст тут — англійський дефолт (переклад help/label/reco в
    # admin/i18n.py, той самий патерн, що й в решти ai-ключів вище).
    "chat_model_light": {
        "group": "ai", "label": "Chat model (light)", "type": "text",
        "help": "Cheaper Anthropic model used for simple navigational chat questions "
                "(find/show/link-style). Empty value disables routing — every question "
                "then uses the main chat model.",
        "reco": "claude-haiku-4-5-20251001 — about 3x cheaper on simple questions; "
                "clear the field if simple-question answers feel too shallow.",
    },
    "chat_cache_ttl_hours": {
        "group": "ai", "label": "Chat answer cache TTL (hours)", "type": "int",
        "min": 0, "max": 720,
        "help": "How long a cached answer to a repeated opening question stays valid. "
                "Within the TTL the same first question of a conversation (same forum "
                "scope, same web flag) is answered from cache at zero token cost. "
                "0 disables the cache.",
        "reco": "24 — the archive updates hourly, but day-old answers to repeated "
                "team questions are almost always still right.",
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

# Серверні пресети /items?view=… (розділ A + B3). На відміну від фільтрів
# вище (комбінуються довільно), пресет — самодостатній, повністю визначений
# зріз: активація ІГНОРУЄ решту query-параметрів, а не додається до них.
# "review24" — банер Миколи для ранкового огляду («digest у Telegram має
# відкриватись у дашборді зібраним»): Telegram-дайджест лінкує сюди напряму
# (те посилання вшиває інший агент, поза цим файлом). "leads24" — плитка
# «New leads (24h)» на /  (розділ B3). Обидва — літерали, не введення
# користувача: `view` лише обирає КЛЮЧ словника, тому інтерполяція значень
# нижче в SQL безпечна так само, як і решта f-string цього файлу (clause).
# WHERE-фрагменти — _LEADS24_WHERE/_REVIEW24_WHERE, визначені разом із
# _leads_badge_context вище: те саме джерело правди, яким тепер рахує і
# NAV-бейдж/плитку «Unread findings» (задача 3 аудиту 2026-08-11).
VIEW_PRESETS: dict[str, dict[str, str]] = {
    "review24": {
        "where": _REVIEW24_WHERE,
        "order": "i.confidence DESC NULLS LAST",
    },
    "leads24": {
        "where": _LEADS24_WHERE,
        "order": "i.first_seen DESC",
    },
}


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
    # `delivered`/`passed_filter` і `outcome=closed` (розширення значення, не
    # новий параметр) — розділ 3 задачі 6 («This week» на Overview): кожне
    # число міні-воронки мусить лінкувати на ТОЙ САМИЙ зріз /items, що дав би
    # цю саму цифру. У видимій формі фільтрів (items.html) цих значень немає
    # — той самий підхід, що й у `view` нижче: deep-лінк без відповідного
    # <select>, як view=leads24/review24 вже були.
    delivered: str = "",
    passed_filter: str = "",
    # Задача 2 аудиту 2026-08-12 — фільтр по власній оцінці «корисно/шум»
    # (seen_items.useful, міграція 014), окремий від outcome (той — доля
    # УГОДИ по ліду, useful — якість самої знахідки; див. коментар
    # set_item_useful нижче).
    useful: str = "",
    view: str = "",
    page: int = 0,
):
    """Знахідки (розділ 4.3, розширення «фільтри + outcomes» + розділ A/B3
    «серверні пресети»).

    Дефолт «усі» НЕ змінюється (F5, той самий інваріант, що й раніше): коли
    жоден параметр не заданий, WHERE лишається порожнім і запит — той самий,
    що й до цієї зміни, тож наявні закладки/посилання з Telegram (`?status=`)
    показують те саме, що й учора.

    `view` (розділ A/B3) — окрема гілка, а не ще один фільтр поверх інших:
    коли він збігається з ключем VIEW_PRESETS, WHERE/ORDER BY повністю
    визначаються пресетом, а status/q/ecosystem/… з query-рядка мовчки
    ІГНОРУЮТЬСЯ (не 400 — та сама філософія терпимості, що й у
    min_confidence/period нижче). Причина: `view=review24` — посилання з
    Telegram-дайджесту, воно мусить показувати рівно той самий зріз щоранку
    незалежно від того, які фільтри людина забула в адресному рядку вчора.
    """
    preset = VIEW_PRESETS.get(view)
    if preset:
        where, params = [preset["where"]], []
        order_by = preset["order"]
        qs_no_page = urlencode({"view": view})
        filters_active = False
    else:
        where, params = [], []
        order_by = "i.first_seen DESC"
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
        elif outcome == "closed":
            where.append("i.outcome IN ('won', 'lost')")
        if delivered == "1":
            where.append("i.delivered_at IS NOT NULL")
        if passed_filter == "1":
            where.append("i.status <> 'filtered'")
        # Задача 2 аудиту 2026-08-12 — «unrated» звужений до status IN
        # ('done','pending'): це і є той самий підмножина рядків, що взагалі
        # отримує кнопки 👍/👎 на items.html (filtered/seeded — «сміття», не
        # оцінюємо), тож фільтр «unrated» не показує рядки, які людина
        # фізично не могла оцінити.
        if useful == "useful":
            where.append("i.useful IS TRUE")
        elif useful == "noise":
            where.append("i.useful IS FALSE")
        elif useful == "unrated":
            where.append("i.useful IS NULL AND i.status IN ('done', 'pending')")

        # Хвіст запиту БЕЗ page — для лінків «новіші/старіші» і для «Скинути».
        # Лише активні фільтри: порожній параметр не повинен смітити URL.
        active_filters = {
            k: v
            for k, v in {
                "status": status, "q": q, "ecosystem": ecosystem, "lane": lane,
                "min_confidence": min_confidence, "period": period, "outcome": outcome,
                "delivered": delivered, "passed_filter": passed_filter,
                "useful": useful,
            }.items()
            if v
        }
        qs_no_page = urlencode(active_filters)
        filters_active = bool(active_filters)

    clause = ("WHERE " + " AND ".join(where)) if where else ""

    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT i.*, s.name AS source_name, s.ecosystem AS source_ecosystem,
                   bf.id AS brief_id
              FROM seen_items i
              JOIN sources s ON s.id = i.source_id
              -- Задача 2 аудиту 2026-08-11: найновіший бріф цього item_uid,
              -- якщо він уже існує — items.html показує лінк «Open brief»
              -- замість форми створення, коли brief_id непорожній.
              LEFT JOIN LATERAL (
                  SELECT b.id FROM kb.briefs b
                   WHERE b.item_uid = i.item_uid
                   ORDER BY b.id DESC LIMIT 1
              ) AS bf ON true
              {clause}
             ORDER BY {order_by} LIMIT 50 OFFSET %s
            """,
            (*params, page * 50),
        ).fetchall()
        # Порожній стан «живий» (план 2026-08-31, дизайн п.3): замість голої
        # таблиці — час останнього збору. Один дешевий SELECT max() по
        # індексованій колонці; воркер ходить щогодини, тож «останній збір
        # о HH:MM» відповідає на головне питання «система взагалі жива?».
        # `or {}` — фейкові курсори тестів віддають None на порожньому
        # наборі, хоч реальний SELECT max() завжди повертає рядок.
        last_run_at = (conn.execute(
            "SELECT max(started_at) AS t FROM worker_runs WHERE mode = 'run'"
        ).fetchone() or {}).get("t")

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
            "useful": useful,
            "view": view,
            "page": page,
            "ecosystem_options": [r["ecosystem"] for r in ecosystem_options],
            "filters_active": filters_active,
            "qs_no_page": qs_no_page,
            "last_run_at": last_run_at,
        },
    )


def _items_prg_target(qs: str) -> str:
    """/items?<qs> для PRG-редіректів цієї секції (set_item_outcome,
    set_item_useful) — одна спільна функція замість дублювання того самого
    «\\r\\n на випадок зіпсованого POST + ліміт довжини» у кожному хендлері.
    `qs` — лише рядок запиту (без "?"), тож ціль завжди лишається на /items
    незалежно від того, що прийшло у формі — відкритого редіректу тут немає
    навіть у теорії."""
    safe_qs = qs.replace("\r", "").replace("\n", "")[:2000]
    return f"/items?{safe_qs}" if safe_qs else "/items"


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

    return RedirectResponse(_items_prg_target(qs), status_code=303)


@mutations.post("/items/{item_uid}/useful")
def set_item_useful(item_uid: str, value: str = Form(...), qs: str = Form("")):
    """👍/👎 на рядках-НЕ-лідах /items (задача 2 аудиту 2026-08-12,
    seen_items.useful — міграція 014).

    НАВМИСНО окреме поле від `outcome` (won/lost вище): outcome — доля
    УГОДИ по ліду (win-rate для калібрування confidence_threshold), useful —
    якість самої знахідки, до й незалежно від того, стала вона лідом чи ні
    (сигнал для порогів/ключових слів на менш зрілому кінці лійки). Змішати
    їх в одному стовпці означало б зіпсувати обидві метрики — тому й кнопки
    рендеряться в різних гілках того самого стовпця Outcome на items.html:
    рядок або лід (Won/Lost), або кандидат на оцінку (👍/👎), ніколи обидва.

    `actor()` тут НЕ пишемо (на відміну від set_item_outcome): useful — це
    сира сигнальна оцінка для подальшої аналітики порогів, а не рішення, за
    яке хтось персонально відповідає перед командою — колонки useful_by тут
    міграція 014 навмисно не додавала.
    """
    if value not in ("yes", "no", "clear"):
        raise HTTPException(400, "value must be 'yes', 'no' or 'clear'")

    with db() as conn:
        if value == "clear":
            conn.execute(
                "UPDATE seen_items SET useful = NULL WHERE item_uid = %s", (item_uid,)
            )
        else:
            conn.execute(
                "UPDATE seen_items SET useful = %s WHERE item_uid = %s",
                (value == "yes", item_uid),
            )
        conn.commit()

    return RedirectResponse(_items_prg_target(qs), status_code=303)


@mutations.post("/items/mark-read")
def mark_items_read(next: str = Form("")):
    """«Mark all as read» (задача 3 — read/unread як у месенджерах, панель
    фільтрів Findings): ставить viewed_at=now() усім НЕПРОЧИТАНИМ глобально
    (viewed_at IS NULL), а не лише видимій сторінці/фільтру — той самий
    принцип, що й «mark all as read» у поштових клієнтах: непрочитане
    ховається звідусіль одразу, а не лише з поточного фільтра.

    Свідомо БЕЗ автоматичного «прочитано при відкритті сторінки» (той самий
    інваріант, що й у коментарі міграції 013): людина сама вирішує, що вже
    переглянула — тому це окрема кнопка, а не побічний ефект GET /items.

    `next` — поточний URL /items (hidden-поле форми, той самий PRG-прийом,
    що й `qs` у set_item_outcome вище): людина повертається туди, звідки
    натиснула кнопку, зі своїми фільтрами й сторінкою пагінації, а не на
    голий /items. Лише локальні цілі, що починаються з "/items" — той самий
    захист від open redirect, що й `next` у delete_chat_session.
    """
    with db() as conn:
        conn.execute("UPDATE seen_items SET viewed_at = now() WHERE viewed_at IS NULL")
        conn.commit()
    target = next if next.startswith("/items") and "//" not in next else "/items"
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


# «Помилка в прогоні» — той самий критерій, що вже рендерить data-fail на
# рядку runs.html (sources_failed АБО непорожній detail->'failures'):
# фільтр ?errors=1 (задача 6 аудиту 2026-08-12) мусить показувати РІВНО ті
# рядки, які й так позначені як проблемні оком, а не власний, розбіжний
# критерій.
_RUN_HAS_ERRORS_WHERE = (
    "(sources_failed > 0 OR jsonb_array_length(coalesce(detail->'failures', '[]'::jsonb)) > 0)"
)


def _render_runs(
    request: Request,
    *,
    mode: str = "",
    limit: int = 50,
    errors: int = 0,
    test_results: list[dict] | None = None,
):
    """Одна точка рендеру /runs — і звичайний GET, і POST /runs/test-sources
    нижче малюють ту саму сторінку (той самий прийом, що й _render_keywords/
    _render_sources: PRG для «Test all sources now» свідомо зламаний,
    результат — ефемерна відповідь живого тест-фетчу, а не стан, що
    зберігається)."""
    limit = 200 if limit == 200 else 50
    where_parts = []
    params: list = []
    if mode:
        where_parts.append("mode = %s")
        params.append(mode)
    if errors:
        where_parts.append(_RUN_HAS_ERRORS_WHERE)
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT *, extract(epoch FROM (finished_at - started_at))::int AS duration_s
              FROM worker_runs {where}
             ORDER BY started_at DESC LIMIT {limit}
            """,
            tuple(params),
        ).fetchall()
        modes = conn.execute(
            "SELECT DISTINCT mode FROM worker_runs ORDER BY mode"
        ).fetchall()
        # Алерти воркера (міграція 016, 2026-08-31): та сама стрічка, що
        # летить пушем у приватний Telegram-бот — але БЕЗ дедупу: тут повна
        # історія, тихне лише пуш (див. worker/alerts.py).
        alerts = conn.execute(
            "SELECT level, message, created_at FROM alerts "
            "WHERE created_at > now() - interval '7 days' "
            "ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
    return templates.TemplateResponse(
        request,
        "runs.html",
        {
            "nav": "runs", "runs": rows, "mode": mode, "limit": limit, "modes": modes,
            "errors": errors, "test_results": test_results, "alerts": alerts,
        },
    )


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, mode: str = "", limit: int = 50, errors: int = 0):
    return _render_runs(request, mode=mode, limit=limit, errors=errors)


def _test_all_sources() -> list[dict]:
    """«Test all sources now» (задача 6 аудиту 2026-08-12): ПЕРЕВИКОРИСТОВУЄ
    `_test_fetch` вище — той самий живий тест-фетч, що й add_source/Sources,
    тож захист netguard (assert_public_url у worker/http.py) лишається на
    місці і жодна SSRF-перевірка тут не обходиться і не дублюється.

    Джерела читаємо ПОВНІШЕ, ніж буквально `id, name, url`: `Source.from_row`
    (worker/fetchers/base.py) вимагає ще й `type`/`ecosystem`, і читає
    `category`/`config`/`lane` — без них `_test_fetch` завжди падав би з
    ValueError ще до першого HTTP-запиту.

    Таймаут 5с на джерело: `HttpClient` (worker/http.py) власного таймауту
    коротшого за конфіг воркера не має, тож 5с рахує ЦЕЙ виклик, окремим
    потоком з `.result(timeout=5)`. Потік, що не встиг, лишається довиконувати
    запит у фоні й тихо завершується сам — Python не вміє вбити потік ззовні,
    а для рідкісної ручної кнопки на адмін-сторінці це прийнятний компроміс
    (на відміну від воркера, тут немає накопичення: наступний клік стартує
    свіжий пул). Послідовно (не паралельно) — навмисно: паралельний шторм
    запитів по всіх джерелах одразу виглядав би для форумів як DDoS.
    """
    import concurrent.futures

    with db() as conn:
        rows = conn.execute(
            "SELECT id, type, name, ecosystem, url, category, config, lane "
            "FROM sources WHERE enabled ORDER BY name"
        ).fetchall()

    results = []
    for row in rows:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_test_fetch, row)
            try:
                count, error = future.result(timeout=5)
            except concurrent.futures.TimeoutError:
                count, error = 0, "timed out after 5s"
        results.append({
            "name": row["name"], "ecosystem": row["ecosystem"],
            "ok": not error, "error": error, "count": count,
        })
    return results


@mutations.post("/runs/test-sources")
def test_all_sources(request: Request):
    """Синхронний запит до ~60с (до 5с × кількість увімкнених джерел,
    послідовно) — кнопка на /runs несе `data-busy` (розділ 3 app.js), інакше
    подвійний клік послав би другий повний прогін паралельно з першим."""
    results = _test_all_sources()
    return _render_runs(request, test_results=results)


# ── Knowledge base ─────────────────────────────────────────────────


# Сентинели підсвітки — керуючі символи, а не «»: `ts_headline` мусить бути
# екранований разом із текстом поста (він може містити літеральний <script>),
# тож теги <mark> підставляє фільтр `hl` уже ПІСЛЯ екранування. Бонус:
# перестають ламатися легітимні лапки «» в українських постах.
HEADLINE_OPTS = "MaxWords=35, MinWords=15, StartSel=\x02, StopSel=\x03"


def _kb_coverage(topics: int, remote_topics: int | None) -> tuple[int | None, str]:
    """Відсоток покриття архіву ТЕМАМИ (задача «покриття архівів», /kb):
    наші теми проти еталону з /about.json форуму (kb.forums.remote_topics,
    міграція 012 — воркер пише його щопрогону). Порахований тут, у Python, а
    не в SQL: NULL (форум ще не мав жодного проходу воркера з еталоном, або
    еталон — 0) інакше довелося б розрізняти від 0% через CASE в кожному
    місці, де читається значення, а тут — одна проста гілка.

    Саме ТЕМИ, а не пости — свідомо (перевірено на проді 2026-08-11):
    /about.json рахує пости, невидимі анонімному API (видалені, whispers,
    приватні категорії), тому пост-метрика показувала б ~50% ВІЧНО навіть
    для повного архіву (Arbitrum: наші 32 457 = сума власних posts_count
    усіх видимих тем, а /about.json заявляє 73 570). Теми чесніші: Arbitrum
    2708/2741 = 99% ✅, ENS 787/2721 = 29% 🔴 — рівно те, що треба бачити.
    Повноту ПОСТІВ у межах відомих тем гарантує окремий механізм —
    kb-repair + CrawlResult.complete у воркері.

    Пороги — з аудиту (2026-08-11): ≥90% достатньо, 60-89% — помітна
    прогалина, <60% — суттєва недостача тем в архіві."""
    if not remote_topics:
        return None, "b-neutral"
    pct = round(100 * topics / remote_topics)
    if pct >= 90:
        return pct, "b-ok"
    if pct >= 60:
        return pct, "b-warn"
    return pct, "b-bad"


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
        # remote_topics/remote_posts/stats_at приходять через f.* вище
        # (міграція 012) — тут лише додаємо готовий відсоток і клас бейджа,
        # щоб шаблон не рахував нічого сам.
        for f in forums:
            f["coverage_pct"], f["coverage_class"] = _kb_coverage(
                f["topics"], f["remote_topics"]
            )

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


def _brief_backend(payload: dict) -> dict:
    """Виокремлено в окрему функцію — той самий прийом, що й _chat_backend/
    _keywords_advice_backend/_chat_report_backend нижче, заради monkeypatch
    у тестах (жоден із них не піднімає kbmcp і не ходить у мережу)."""
    import httpx

    response = httpx.post(
        f"{KBMCP_URL}/brief",
        json=payload,
        headers={"Authorization": f"Bearer {KB_MCP_TOKEN}"} if KB_MCP_TOKEN else {},
        timeout=300,  # LLM tier legitimately takes minutes
    )
    return response.json()


@mutations.post("/items/{item_uid}/brief")
def generate_brief(item_uid: str, model: str = Form("")):
    """Manual trigger — the same call the n8n node makes after lead creation.

    `model` (задача 4 аудиту 2026-08-11): компактний <select> у рядку
    items.html — "" (Default), "claude-sonnet-5" чи "claude-opus-5"
    (BRIEF_MODEL_CHOICES/BRIEF_MODEL_ALLOWED вище). Лише вайтлістове
    значення потрапляє в payload до kbmcp; порожнє чи невідоме — ключ
    "model" туди взагалі не йде, і kbmcp сам падає на settings.brief_model
    (контракт POST /brief: опціональне поле, невалідне ігнорується).
    """
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

    payload = {
        "ecosystem": item["ecosystem"],
        "title": item["title"] or item_uid[:16],
        "body": item["body"] or "",
        "item_uid": item_uid,
    }
    if model in BRIEF_MODEL_ALLOWED:
        payload["model"] = model

    try:
        result = _brief_backend(payload)
    except (httpx.HTTPError, ValueError) as exc:
        return RedirectResponse(f"/items?status=&source_id=0#brief-error-{exc.__class__.__name__}",
                                status_code=303)

    if result.get("error"):
        # No archive for this ecosystem — the honest outcome, show it inline.
        return RedirectResponse("/items", status_code=303)
    return RedirectResponse(f"/briefs/{result['brief_id']}", status_code=303)


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


def _share_token_valid(brief_id: int, token: str) -> bool:
    """Перевірка magic-токена з Telegram-лінка (видає mcp/weekly.share_token:
    "<expiry_unix>.<hmac_sha256(brief_id|expiry)[:32]>").

    Ключ — KB_MCP_TOKEN: він уже є в env ОБОХ сервісів (kbmcp підписує,
    admin перевіряє), окремий спільний секрет не потрібен. Fail-closed:
    без токена в env перевірка завжди хибна — magic-лінки просто не
    працюють, а не працюють БЕЗ підпису. compare_digest — проти таймінгу,
    як і скрізь у auth.py."""
    secret = os.environ.get("KB_MCP_TOKEN", "")
    if not secret or token.count(".") != 1:
        return False
    expiry_s, _, sig = token.partition(".")
    if not expiry_s.isdigit() or int(expiry_s) < time.time():
        return False
    expected = hmac.new(secret.encode(), f"{brief_id}|{expiry_s}".encode(),
                        hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, expected)


@app.get("/share/briefs/{brief_id}", response_class=HTMLResponse)
def share_brief_page(request: Request, brief_id: int, t: str = ""):
    """Read-only перегляд ОДНОГО бріфа за magic-лінком, БЕЗ сесії (UX-план
    2026-08-31 п.2). Шлях під /share/ — публічним префіксом auth.PUBLIC_PREFIX,
    тож middleware його не чіпає: автентифікація тут — сам підписаний токен.

    Свій шаблон (share_brief.html), а не brief.html: без сайдбара і дій —
    людина без сесії не має бачити навігацію, що вся веде на логін-стіну,
    ані кнопок archive/delete, які все одно відіб'ються CSRF-ом. Чистий
    документ + пропозиція увійти для повної версії."""
    if not _share_token_valid(brief_id, t):
        # 404, не 403: не підтверджуємо існування бріфа тому, хто підбирає id.
        raise HTTPException(404, "not found")
    with db() as conn:
        brief = conn.execute(
            "SELECT * FROM kb.briefs WHERE id = %s", (brief_id,)
        ).fetchone()
    if not brief:
        raise HTTPException(404, "not found")
    response = templates.TemplateResponse(
        request, "share_brief.html", {"brief": brief}
    )
    # PUBLIC-шляхи виходять із middleware до кроку no-store — ставимо самі:
    # лінк живе в груповому чаті, кешувати відповідь поза браузером не можна.
    response.headers["Cache-Control"] = "private, no-store"
    return response


@mutations.post("/deadlines/{deadline_id}/dismiss")
def dismiss_deadline(deadline_id: int):
    """«Прибрати з очей» рядок Closing soon (подали заявку / нерелевантно):
    рядок лишається в kb.deadlines для історії, а upsert зі щотижневого
    звіту свідомо не чіпає dismissed_at — повторна згадка програми у звіті
    не повертає її на Overview."""
    with db() as conn:
        conn.execute(
            "UPDATE kb.deadlines SET dismissed_at = now() "
            "WHERE id = %s AND dismissed_at IS NULL",
            (deadline_id,),
        )
        conn.commit()
    return RedirectResponse("/", status_code=303)


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


# Групи випадаючого списку форумів у композері (розділ 3 редизайну чату,
# рішення Миколи 2026-08-11 — замінює колишні форумні чипи над композером).
# kb.forums.kind обмежений CHECK-констрейнтом до цих чотирьох значень
# (migrations/008_kb_kinds_snapshot.sql, 010_kb_github.sql) — порядок і
# людські підписи фіксовані тут, а не в SQL ORDER BY: бажаний порядок показу
# (Forums → Snapshot → GitHub → Sites) не збігається з алфавітним.
CHAT_SCOPE_KIND_ORDER = ["discourse", "snapshot", "github", "site"]
CHAT_SCOPE_KIND_LABELS = {
    "discourse": ("pg.chat.scope.discourse", "Forums"),
    "snapshot": ("pg.chat.scope.snapshot", "Snapshot"),
    "github": ("pg.chat.scope.github", "GitHub"),
    "site": ("pg.chat.scope.site", "Sites"),
}


def _forum_scope_groups(rows: list[dict]) -> list[dict]:
    """Групує enabled-рядки kb.forums за `kind` у фіксованому порядку вище —
    випадаючий список композера (chat.html: .chat__scope) показує форуми під
    заголовками «Forums»/«Snapshot»/«GitHub»/«Sites», а не плоским списком.

    Невідомий `kind` (майбутня міграція додасть новий) не губиться — той
    самий інваріант F13, що й STATUS_UA/LANE_UA/… вище: йде останньою
    групою під власною, непере кладеною назвою, а не зникає мовчки.
    """
    by_kind: dict[str, list[str]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r["forum_slug"])
    groups = [
        {"label_key": CHAT_SCOPE_KIND_LABELS[kind][0], "label_en": CHAT_SCOPE_KIND_LABELS[kind][1],
         "slugs": by_kind[kind]}
        for kind in CHAT_SCOPE_KIND_ORDER if kind in by_kind
    ]
    groups += [
        {"label_key": f"pg.chat.scope.{kind}", "label_en": kind.capitalize(), "slugs": slugs}
        for kind, slugs in by_kind.items() if kind not in CHAT_SCOPE_KIND_ORDER
    ]
    return groups


# Валідація checkbox-значень "forums" у POST /chat/send нижче — той самий
# алфавіт, що й forum_slug у БД (migrations/004_kb_schema.sql: text, свідомо
# без формального CHECK на боці Postgres). Невалідне значення тут можливе
# лише при ручному підробленні тіла POST (композер рендерить чекбокси лише
# з реальних enabled-рядків) — тому мовчки відкидається, а не 400.
_FORUM_SLUG_RE = re.compile(r"^[a-z0-9-]{1,64}$")
CHAT_SCOPE_MAX_FORUMS = 12

# Значення каналу чату — той самий алфавіт, що й kb.chat_messages.channel
# (migrations/006_kb_chat.sql). Визначено тут (а не поруч із /chats нижче,
# де він жив раніше), бо й save_chat_report (розділ D нижче), і chat_page
# (розділ AI-чату вище — визначає, чи ?hist=<key> веде на ЖИВУ web-розмову
# чи read-only архів) валідують канал, розпарсений з ключа "channel:session"
# — усі споживачі мають бачити той самий список.
CHAT_CHANNEL_OPTIONS = ("web", "telegram")


def _parse_chat_history_key(key: str) -> tuple[str, str] | None:
    """→ (channel, session) з архівного ключа "channel:session" (розділ D
    задачі 5 аудиту 2026-08-11 — «Create Brief» з архівної розмови /chat?
    hist=<key>) або None, якщо ключ невалідний.

    Спліт по ПЕРШІЙ двокрапці (`partition`, не `split(":", 1)` заради
    симетрії з рештою файлу): channel мусить бути одним із
    CHAT_CHANNEL_OPTIONS, а session — непорожнім і без ВЛАСНОЇ двокрапки
    (той самий контракт, що session_key має в kb.chat_messages: неймспейс —
    рівно один префікс "канал:", а не довільна кількість). `hist_key` у
    chat.html завжди прийшов ІЗ хендлера chat_page (він сам будує
    session_key = "web:" + sid чи читає channel/session_key з БД), тож
    невалідне значення тут можливе лише при ручному підробленні POST-тіла.
    """
    channel, sep, session = key.partition(":")
    if not sep or channel not in CHAT_CHANNEL_OPTIONS or not session or ":" in session:
        return None
    return channel, session


@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request, error: str = "", ask: str = "", hist: str = ""):
    """AI-чат над базою знань (розділ 4.9, задача #30; редизайн 2026-08-11 —
    постійна панель історії СПРАВА замість колишньої шухляди-по-URL; задача
    «Продовжувані розмови» 2026-08-28 — розмову визначає URL, а не cookie:
    раніше БУДЬ-яка чужа/минула web-розмова була назавжди read-only, тепер
    архівною лишається лише telegram, бо дашборд ФІЗИЧНО не може відповісти
    telegram-юзеру).

    `hist` лишається станом у URL (інваріант «кожна фіча працює без JS»):
    порожній — жива розмова цього браузера (сторінка = композер + список
    повідомлень, під cookie sid, розділ _chat_key). Непорожній парситься
    _parse_chat_history_key на (channel, session):
      - channel == "web" — ТЕЖ жива розмова, лише під ІНШИМ session, ніж
        cookie sid цього браузера: композер лишається видимим, форма несе
        hidden `session` = `continue_key` (chat.html), звідки POST
        /chat/send нижче читає його назад і дописує повідомлення саме в
        ЦЮ розмову, а не заводить нову під cookie sid.
      - channel == "telegram", або ключ узагалі не парситься (ручне
        підроблення URL) — старий режим: read-only архів, композер
        прихований, банер зверху («Archived conversation» + «Back to
        current chat»).
    Колишня семантика "hist=1" (окремий «список розмов» усередині шухляди)
    зникла разом із шухлядою: список тепер завжди видно в .chat-side
    праворуч, тож "1" просто ігнорується — сторінка поводиться як голий
    /chat.

    `ask` (розділ D2) лише ПРЕФІЛИТЬ композер на сервері — жодного
    автосабміту: людина сама вирішує, надсилати питання чи спершу
    відредагувати. Обрізання до 4000 — той самий ліміт, що й у POST
    /chat/send. Textarea в chat.html підставляє значення між тегами —
    автоескейпінг Jinja вже покриває XSS, окремого екранування тут не треба.
    """
    who = auth.session_who(request)
    session_key = f"web:{_chat_key(request)}"
    ask = ask[:4000]
    hist_key = hist if hist and hist != "1" else ""

    # continue_key — сирий session (БЕЗ "web:") продовжуваної ЧУЖОЇ/минулої
    # web-розмови, іде в шаблон для hidden-поля композера — звідти POST
    # /chat/send читає його назад як Form("session"). Порожній в решті
    # випадків: жива сесія цього браузера і так пише під власним cookie sid
    # без потреби у явному session-полі.
    parsed_hist = _parse_chat_history_key(hist_key) if hist_key else None
    continue_key = parsed_hist[1] if parsed_hist and parsed_hist[0] == "web" else ""
    # Read-only архів лишається лише для telegram і непарсабельних ключів —
    # web-розмова за ЧУЖИМ ключем (continue_key непорожній) тепер ЖИВА.
    archived = bool(hist_key) and not continue_key

    with db() as conn:
        if archived:
            # Read-only чужа/минула розмова — ті самі колонки, що й жива
            # нижче, тож шаблон рендерить обидві гілки одним циклом бульбашок.
            messages = conn.execute(
                "SELECT id, role, who, content, tier, model, created_at "
                "FROM kb.chat_messages WHERE session_key = %s ORDER BY id LIMIT 300",
                (hist_key,),
            ).fetchall()
            forum_groups: list[dict] = []
        else:
            # Жива розмова: або cookie-сесія цього браузера (continue_key
            # порожній, hist_key теж), або продовжувана web-розмова за
            # ключем із URL (continue_key непорожній, read_key == hist_key
            # == "web:<session>") — той самий SELECT, лише інший ключ.
            read_key = hist_key if continue_key else session_key
            messages = conn.execute(
                "SELECT id, role, who, content, tier, model, created_at "
                "FROM kb.chat_messages WHERE session_key = %s ORDER BY id LIMIT 200",
                (read_key,),
            ).fetchall()
            forum_rows = conn.execute(
                "SELECT forum_slug, kind FROM kb.forums WHERE enabled ORDER BY kind, forum_slug"
            ).fetchall()
            forum_groups = _forum_scope_groups(forum_rows)

        # Панель історії СПРАВА (розділ 1 редизайну): рендериться ЗАВЖДИ, а
        # не лише за колишнім ?hist=1 — LIMIT 30, той самий агрегуючий запит,
        # що раніше жив під шухлядою (без sum(tokens) — панель показує лише
        # прев'ю/мету/кількість питань, детальні токени лишились на /chats).
        hist_sessions = conn.execute(
            """
            SELECT session_key, channel,
                   max(created_at) AS last_at,
                   count(*) FILTER (WHERE role = 'user') AS questions,
                   max(who) FILTER (WHERE role = 'user') AS who,
                   left((array_agg(content ORDER BY id) FILTER (WHERE role = 'user'))[1], 60) AS preview
              FROM kb.chat_messages
             GROUP BY session_key, channel
             ORDER BY last_at DESC LIMIT 30
            """
        ).fetchall()

    for s in hist_sessions:
        s["hist_href"] = "/chat?hist=" + quote(s["session_key"], safe="")
        s["view_href"] = "/chats/view?key=" + quote(s["session_key"], safe="")
        # ВІДКРИТА розмова — hist_key, якщо є (архівна чи продовжувана),
        # інакше cookie-сесія цього браузера: та сама умова, що й read_key
        # вище, тож підсвічення завжди вказує саме на те, що зараз видно.
        s["is_active"] = s["session_key"] == (hist_key or session_key)

    # Останній рядок — user: відповідь, можливо, ще генерується (POST
    # /chat/send блокується до 300 с — той самий таймаут, що й у /brief) в
    # ІНШІЙ вкладці чи запиті, який саме зараз обробляється. Рядок-натяк, а
    # не спінер із поллінгом — автополінг заборонений (app.js, розділ 0):
    # він тихо продовжував би сесію без участі людини. Не для read-only
    # архіву (там композера, який міг би це запустити, нема) — але тепер
    # показується і для продовжуваної web-розмови: вона теж жива.
    thinking = (not archived) and bool(messages) and messages[-1]["role"] == "user"

    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "nav": "chat",
            "who": who,
            "messages": messages,
            "thinking": thinking,
            "error": error,
            "ask": ask,
            "archived": archived,
            "hist_key": hist_key,
            "continue_key": continue_key,
            "hist_sessions": hist_sessions,
            "forum_groups": forum_groups,
        },
    )


@mutations.post("/chat/send")
def send_chat_message(
    request: Request,
    message: str = Form(...),
    web: str = Form(""),
    forums: list[str] = Form(default=[]),
    session: str = Form(""),
):
    """Одне повідомлення → один запит до kbmcp.

    Відповідь гілкується заголовком X-Requested-With: fetch — його ставить
    лише app.js (звичайний браузер його не додає), тож без JS форма завжди
    йде по PRG-контракту (303 на /chat), а з JS повертається JSON. Помилки
    kbmcp НЕ ковтаються мовчки в редірект без деталей (на відміну від
    generate_brief — там немає JS-гілки, і фрагмент у URL прийнятний
    компроміс): тут є куди показати причину, і ковтати її означало б
    видавати порожню відповідь замість пояснення.

    `session` (задача «Продовжувані розмови» 2026-08-28): hidden-поле
    композера, непорожнє лише коли форма відкрита з ПРОДОВЖУВАНОЇ web-
    розмови (chat_page: `continue_key`, ?hist=web:<session>) — тоді
    повідомлення пишеться в ТУ розмову, а не в нову під cookie sid цього
    браузера. Валідація тут ЖОРСТКА (400), на відміну від м'якого
    відкидання `forums` нижче: невалідний/чужий session означає, що
    розмову ГЕНУЇННО не можна продовжити — тихе ігнорування замишляло б
    повідомлення в НОВУ (не ту) розмову, і людина цього не помітила б.
    Перевіряється: без ':' (той самий контракт, що й у самого session_key —
    рівно один префікс "channel:"), довжина ≤ 64, і "web:<session>" реально
    ІСНУЄ в kb.chat_messages (інакше це або підроблений POST, або сесія,
    якої вже стерли/не було). Успішний `session` також зсуває PRG-редірект
    (no-JS шлях) із голого /chat на /chat?hist=web:<session> — і при
    успіху, і при ПОМИЛЦІ (fail() нижче): без JS людина мусить лишитися в
    тій самій продовженій розмові, а не втратити її з очей на порожньому
    /chat.

    `web` (розділ C — тумблер веб-пошуку в composer chat.html): checkbox
    `name="web" value="1"`, тож непозначений чекбокс браузер узагалі НЕ
    надсилає (стандартна поведінка форм) — `Form("")` ловить обидва випадки
    однаково. У payload kbmcp ключ "web" з'являється ЛИШЕ коли позначено:
    контракт kbmcp — опціональне булеве поле, і «відсутній» та «false»
    рівнозначні для нього, тож нема сенсу засмічувати payload зайвим ключем
    на КОЖНЕ повідомлення (переважна більшість — без веб-пошуку).

    `forums` (розділ 3 редизайну — випадаючий список форумів у композері):
    чекбокси `name="forums" value="<slug>"` усередині ТІЄЇ Ж форми, тож
    FormData (app.js) підхоплює їх без окремого коду. Валідація тут — той
    самий принцип м'якого відкидання, що й у add_source/validate_setting:
    невалідний slug чи зайвий понад CHAT_SCOPE_MAX_FORUMS просто випадає зі
    списку, а не валить запит 400-кою — композер рендерить чекбокси лише з
    реальних enabled-рядків kb.forums, тож невалідне значення тут можливе
    лише при ручному підробленні тіла POST. Порожній підсумковий список —
    ключа "forums" у payload узагалі нема (той самий підхід, що й "web"
    вище): «нічого не обрано» і «поле відсутнє» рівнозначні для kbmcp.
    """
    # СИРИЙ ключ, без префікса "web:" — kbmcp неймспейсить сам при записі
    # (контракт /chat забороняє ':' у session_key саме для того, щоб веб не
    # міг адресувати telegram-сесії). Префікс додається лише при ЧИТАННІ
    # kb.chat_messages у chat_page — там ключ уже збережений неймспейснутим.
    # За замовчуванням — cookie-сесія цього браузера; валідований `session`
    # нижче перекриває її на продовжувану розмову.
    session_key = _chat_key(request)
    who = auth.session_who(request)
    is_fetch = request.headers.get("X-Requested-With") == "fetch"

    # Куди веде PRG-редірект (no-JS) при ПОМИЛЦІ чи УСПІХУ: "" — голий
    # /chat (дефолт), "web:<session>" — ПІСЛЯ успішної валідації `session`
    # нижче, щоб і невдача (порожнє повідомлення, недоступний kbmcp) не
    # викидала людину з продовженої розмови на дефолтну сесію.
    redirect_hist = ""

    def fail(err: str, status_code: int = 400):
        if is_fetch:
            return JSONResponse({"ok": False, "error": err}, status_code=status_code)
        params = {"error": err}
        if redirect_hist:
            params["hist"] = redirect_hist
        return RedirectResponse(f"/chat?{urlencode(params)}", status_code=303)

    if session:
        # Жорсткий 400, не м'яке відкидання (як для forums нижче) — хибний
        # session означав би мовчазне замишлення повідомлення в НОВУ
        # розмову замість продовження старої, і людина цього не помітила б.
        if ":" in session or len(session) > 64:
            return fail("This conversation cannot be continued", 400)
        with db() as conn:
            exists = conn.execute(
                "SELECT 1 FROM kb.chat_messages WHERE session_key = %s LIMIT 1",
                (f"web:{session}",),
            ).fetchone()
        if not exists:
            return fail("This conversation cannot be continued", 400)
        session_key = session
        redirect_hist = f"web:{session}"

    text = message.strip()
    if not text:
        return fail("Message is empty")
    if len(text) > 4000:
        return fail(f"Message is too long — {len(text)} characters (limit is 4000)")

    import httpx

    scoped_forums = [f for f in forums if _FORUM_SLUG_RE.match(f)][:CHAT_SCOPE_MAX_FORUMS]

    body = {"channel": "web", "session_key": session_key, "who": who, "message": text}
    if web:
        body["web"] = True
    if scoped_forums:
        body["forums"] = scoped_forums

    try:
        payload = _chat_backend(body)
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
    if redirect_hist:
        return RedirectResponse(f"/chat?{urlencode({'hist': redirect_hist})}", status_code=303)
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

    Guard — рівно один: role == 'assistant' і tier == 'llm'. Кнопка в
    chat.html рендериться лише під такими бульбашками, але сама форма шле
    голий message_id — без цієї перевірки підміна id в DevTools дала б
    зберегти чиєсь питання (role='user') чи keyword-рівня відповідь без
    жодної LLM-синтези за нею.

    Колишній guard 2 (session_key рядка == сесія ЦЬОГО браузера) прибрано
    задачею «Продовжувані розмови» 2026-08-28: дашборд — команда на одному
    спільному паролі («Chat history (archive)» нижче), уся kb.chat_messages
    і так уже читається будь-ким без розмежування «своя/чужа» сесія (/chats,
    /chat?hist=<key>), а /chat/save-report (сусідній хендлер, «Create Brief»
    з ЦІЛОЇ розмови) вже дозволяв перетворити на бріф будь-яку розмову,
    телеграмну включно. Той guard захищав мутацію, що не приховувала нічого,
    чого й так не видно поруч — і водночас ламав «Save as brief» усередині
    ПРОДОВЖЕНОЇ (не своєї) web-розмови. Замість нього — легка defensive-
    перевірка, що session_key рядка взагалі має вигляд "channel:session"
    (_parse_chat_history_key, той самий парсер, що й у /chat?hist= і
    /chat/save-report): kbmcp завжди пише саме такий формат, тож на
    практиці це пропускає БУДЬ-яке реальне повідомлення, що пройшло guard 1
    — це лише страховка від пошкодженого рядка, а не розмежування доступу.
    Обидва провали віддають однаковий 404 «message not found», а не окремі
    403/400: розрізняти для викликача немає сенсу — жодна з причин не є тим,
    що людина виправляє повторним кліком.
    """
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
            or not _parse_chat_history_key(row["session_key"])
        ):
            raise HTTPException(404, "message not found")

        # Заголовок бріфа — з питання людини, що передувало цій відповіді, у
        # ТІЙ САМІЙ розмові, що й сам рядок (row["session_key"] — а НЕ сесія
        # браузера, що зараз тисне кнопку: вони більше не обов'язково
        # збігаються після видалення guard 2 вище). «Chat answer» — чесний
        # фолбек для найпершого рядка сесії, де попереднього повідомлення
        # просто не існує.
        preceding = conn.execute(
            "SELECT content FROM kb.chat_messages "
            "WHERE session_key = %s AND id < %s AND role = 'user' "
            "ORDER BY id DESC LIMIT 1",
            (row["session_key"], message_id),
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


def _chat_report_backend(payload: dict) -> dict:
    """Виокремлено в окрему функцію — той самий прийом, що й _chat_backend/
    _keywords_advice_backend вище, заради monkeypatch у тестах.

    Контракт kbmcp /chat-brief (розділ D — «Зберегти ЧАТ як звіт»): бріф із
    УСІЄЇ поточної розмови, а не з однієї репліки (на відміну від
    /chat/save-brief нижче, яка сама пише в kb.briefs без походу в kbmcp —
    там джерело вже готовий текст ОДНІЄЇ бульбашки, тут kbmcp мусить сам
    прочитати всю історію сесії й синтезувати підсумок).
    """
    import httpx

    response = httpx.post(
        f"{KBMCP_URL}/chat-brief",
        json=payload,
        headers={"Authorization": f"Bearer {KB_MCP_TOKEN}"} if KB_MCP_TOKEN else {},
        timeout=300,  # LLM-синтез над цілою розмовою — той самий контракт, що й /chat, /brief
    )
    return response.json()


@mutations.post("/chat/save-report")
def save_chat_report(request: Request, model: str = Form(""), key: str = Form("")):
    """«Create Brief» (розділ D, кнопка поруч із «Send» у композері; розділ 5
    задачі аудиту 2026-08-11 — перейменовано з «Save chat as report», і тепер
    доступна й з архівного перегляду /chat?hist=<key>): на відміну від
    /chat/save-brief (одна бульбашка асистента), тут бріф синтезує kbmcp з
    УСІЄЇ розмови.

    `key` (задача 5): порожній — жива сесія ЦЬОГО браузера, той самий
    контракт, що й раніше (`_chat_key`, СИРИЙ ключ без "web:" — kbmcp сам
    неймспейсить при записі в kb.chat_messages). Непорожній —
    "channel:session" з архівного `hist_key` (chat.html: hidden `key=`
    поруч із кнопкою в банері «Archived conversation»), розпарсений
    _parse_chat_history_key вище; команда бачить УСІ розмови в /chats
    (свідома відсутність розмежування «своя/чужа» — той самий коментар, що
    й над GET /chats нижче), тож бріф із чужої розмови тут консистентний.
    Невалідний ключ (ручне підроблення POST — форма завжди шле лише те, що
    сама ж chat_page поклала в hist_key) — редірект на /chat з помилкою,
    жодного походу в kbmcp.

    `model` (задача 4): той самий вайтліст BRIEF_MODEL_ALLOWED, що й
    generate_brief вище — порожнє чи невідоме значення не потрапляє в
    payload, kbmcp сам падає на settings.brief_model.

    Кнопка в chat.html рендериться лише коли `messages` непорожні, але
    хендлер про це не знає (він не бачить стану сторінки, що вже
    відрендерилась) — порожня розмова для kbmcp просто помилка «нічого
    синтезувати», а не окрема гілка тут.
    """
    if key:
        parsed = _parse_chat_history_key(key)
        if not parsed:
            return RedirectResponse(
                "/chat?" + urlencode({"error": "Invalid conversation reference"}),
                status_code=303,
            )
        channel, session_key = parsed
    else:
        channel, session_key = "web", _chat_key(request)

    payload = {"channel": channel, "session_key": session_key}
    if model in BRIEF_MODEL_ALLOWED:
        payload["model"] = model

    import httpx

    try:
        result = _chat_report_backend(payload)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("kbmcp /chat-brief unreachable: %s", exc)
        return RedirectResponse(
            "/chat?" + urlencode({
                "error": "Report backend is unreachable — please try again in a moment",
            }),
            status_code=303,
        )

    if not result.get("ok"):
        err = result.get("error") or "Could not generate the report right now"
        return RedirectResponse(f"/chat?{urlencode({'error': err})}", status_code=303)

    return RedirectResponse(f"/briefs/{result['brief_id']}", status_code=303)


# ── Chat history (archive) ────────────────────────────────────────
#
# Команда ділить один пароль дашборда — тож весь kb.chat_messages видно всім,
# з ОБОХ каналів (веб-композер вище і Telegram-бот): свідома командна
# домовленість (не діра), зафіксована тут коментарем, а не десь у чаті поза
# кодом. На відміну від /chat/save-brief вище, де guard на session_key
# захищає саме МУТАЦІЮ (створення бріфа під чужим ім'ям), тут лише читання —
# розмежовувати «моя сесія / чужа» нема від чого захищати.
#
# Обидва маршрути — прості GET, які лише читають kb.chat_messages (як і /chat
# вище); нічого не пишуть, тож лишаються поза роутером `mutations`.
# CHAT_CHANNEL_OPTIONS — визначено раніше у файлі (поруч із _chat_key), бо
# save_chat_report (розділ D вище) теж валідує канал з архівного ключа
# "channel:session" за цим самим списком.


@app.get("/chats", response_class=HTMLResponse)
def chats_page(request: Request, channel: str = "", period: str = ""):
    """Список чат-сесій: один рядок — одна сесія (session_key), згорнута з
    kb.chat_messages однією агрегуючою вибіркою (без N+1 у Python).

    `period` фільтрує за ОСТАННЬОЮ активністю сесії (`HAVING max(created_at)
    > ...`), а не за окремими повідомленнями — інакше стара сесія з однією
    свіжою реплікою показала б `started`, обрізаний межею періоду, і виглядала
    б коротшою, ніж є насправді. `channel`, навпаки, фільтрує в WHERE — він
    сталий для всієї сесії (session_key завжди в межах одного каналу), тож
    звужувати вибірку до GROUP BY дешевше, ніж відкидати вже згорнуті групи.
    """
    where, params = [], []
    if channel in CHAT_CHANNEL_OPTIONS:
        where.append("channel = %s")
        params.append(channel)
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    having = ""
    if period == "7d":
        having = "HAVING max(created_at) > now() - interval '7 days'"
    elif period == "30d":
        having = "HAVING max(created_at) > now() - interval '30 days'"

    with db() as conn:
        # `array_agg(content ORDER BY id) FILTER (...)` [1] — найстаріше
        # повідомлення user у групі: FILTER на агрегаті, без окремого JOIN чи
        # LATERAL, залишається однією вибіркою (як і `max(who) FILTER (...)`
        # поруч — той самий прийом для «хто питав»).
        rows = conn.execute(
            f"""
            SELECT session_key, channel,
                   min(created_at) AS started,
                   max(created_at) AS last_at,
                   count(*) AS messages,
                   count(*) FILTER (WHERE role = 'user') AS questions,
                   coalesce(sum(coalesce(tokens_in, 0) + coalesce(tokens_out, 0)), 0) AS tokens,
                   max(who) FILTER (WHERE role = 'user') AS who,
                   left((array_agg(content ORDER BY id) FILTER (WHERE role = 'user'))[1], 80) AS preview
              FROM kb.chat_messages
              {clause}
             GROUP BY session_key, channel
             {having}
             ORDER BY last_at DESC LIMIT 100
            """,
            params,
        ).fetchall()

    # Ключ сесії містить ':' (web:<sid> / telegram:<id>[-ts]) — quote(safe="")
    # той самий прийом, що й ask_ai_href на /items вище.
    for row in rows:
        row["view_href"] = "/chats/view?key=" + quote(row["session_key"], safe="")

    return templates.TemplateResponse(
        request,
        "chats.html",
        {
            "nav": "chats",
            "sessions": rows,
            "channel": channel,
            "period": period,
            "filters_active": bool(channel or period),
        },
    )


@app.get("/chats/view", response_class=HTMLResponse)
def chat_view_page(request: Request, key: str = ""):
    """Одна сесія повністю, read-only — та сама архівна логіка, що й
    /briefs/{id} для бріфів: сторінка лише показує, нічого не пише.

    Порожній чи невідомий `key` дають ОДНАКОВИЙ дружній порожній стан замість
    500: `rows` лишається порожнім списком в обох випадках, і хендлер навіть
    не йде в БД, коли key взагалі не передано.
    """
    rows = []
    if key:
        with db() as conn:
            rows = conn.execute(
                """
                SELECT id, channel, role, who, content, tier, model,
                       tokens_in, tokens_out, created_at
                  FROM kb.chat_messages
                 WHERE session_key = %s
                 ORDER BY id LIMIT 500
                """,
                (key,),
            ).fetchall()

    return templates.TemplateResponse(
        request,
        "chat_view.html",
        {
            "nav": "chats",
            "session_key": key,
            "channel": rows[0]["channel"] if rows else "",
            "messages": rows,
            "tokens": sum((r["tokens_in"] or 0) + (r["tokens_out"] or 0) for r in rows),
        },
    )


@mutations.post("/chats/delete")
def delete_chat_session(request: Request, key: str = Form(""), next: str = Form("")):
    """Видалення однієї розмови (запит Миколи 2026-08-11: історія цінна, але
    має прибиратися «по ненадобності»). Незворотне — тому кнопка в шаблоні
    під data-confirm, а маршрут на mutations (сесія + CSRF автоматично).

    `next` — куди повертатись (шухляда історії в /chat чи повна сторінка).
    Тільки локальні цілі, що починаються з "/chat" — усе інше ігнорується:
    open-redirect через hidden-поле форми не потрібен нікому хорошому.
    """
    if key:
        with db() as conn:
            conn.execute(
                "DELETE FROM kb.chat_messages WHERE session_key = %s", (key,)
            )
            conn.commit()
    target = next if next.startswith("/chat") and "//" not in next else "/chats"
    return RedirectResponse(target, status_code=303)


@mutations.post("/chats/prune")
def prune_chat_sessions(request: Request):
    """Прибирання всієї історії, старшої за 30 днів, одним рухом — щоб список
    не заростав ручним видаленням по одній розмові."""
    with db() as conn:
        conn.execute(
            "DELETE FROM kb.chat_messages WHERE created_at < now() - interval '30 days'"
        )
        conn.commit()
    return RedirectResponse("/chats", status_code=303)


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
