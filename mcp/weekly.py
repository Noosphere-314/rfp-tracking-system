"""Щотижневі AI-звіти — понеділковий крон (цілі Паші №2-3, 2026-08-28).

Два види, обидва пишуться рядком у kb.briefs (і тому самі з'являються на
сторінці Briefs дашборда, без жодного нового UI):

  grants     «Weekly EVM grants & RFPs» — нові/оновлені грантові програми,
             RFP і foundation missions по всьому EVM (нові чейни включно),
             дедлайни, і короткий game plan: на що бідити цього тижня.
  discovery  «Weekly EVM discovery» — нові чейни/продукти/апдейти,
             funding-раунди як buying signals, і які форуми варто ДОДАТИ
             в базу знань (проти списку вже відстежуваних).

Виклик — POST /weekly-report із n8n-крону rfp-weekly (понеділок 9:00).

Пайплайн свідомо ЛІНІЙНИЙ, без agentic-циклу briefing._llm_brief:
  1) веб-крок — ОКРЕМИЙ messages.create лише з серверним web_search
     (правило з chat._web_research: server-tool НЕ МОЖНА класти в один
     цикл із клієнтськими інструментами — протокол ламається на
     незавершеному серверному пошуку); провал кроку НЕ валить звіт —
     архівного контексту досить для чесного «тиждень тонкий»;
  2) kb-крок — детермінований, без LLM: кілька keyword-пошуків
     kbtools.search_impl + теми, підняті за останні 7 днів (+ для
     discovery — список відстежуваних форумів, щоб модель пропонувала
     лише ще НЕ підключені);
  3) синтез — один messages.create БЕЗ інструментів.

На відміну від briefing.make_brief тут НЕМАЄ basic-тира: щотижневий звіт
без LLM — це просто звалище сирих сніпетів, яке СЕО читати не буде;
відсутній ANTHROPIC_API_KEY → чесна відмова 503, n8n донесе її в Telegram.

Захист від повторного запуску: якщо звіт цього виду вже створено за
останні 3 доби — віддаємо його id зі skipped=true, LLM не викликається.
Це страховка від подвійного пострілу крону і від відомої грабки
n8n-імпорту, що плодить дублікати воркфлоу.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import date

import psycopg
from psycopg.rows import dict_row

import kbtools

log = logging.getLogger("kb-weekly")

DATABASE_URL = os.environ["DATABASE_URL"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Своя копія (briefing._MODEL_RE): модулі mcp/ свідомо не імпортують один
# одного заради констант — kbmcp деплоїться як плоска купка файлів, і
# зайва зв'язність тут дорожча за 1 рядок дублю (той самий аргумент, що
# й у chat._brief_max_words).
_MODEL_RE = re.compile(r"claude-[a-z0-9.-]{3,40}")

# Своя копія chat._WEB_SEARCH_TOOL — та сама версія 20260209 (НЕ 20260318
# з доків: той із сімейства code-execution і вимагає container_id — прод
# уже падав на ньому 2026-08-11, див. коментар у chat.py). max_uses вищий,
# ніж у чаті: тижневий огляд легітимно потребує кількох пошуків.
_WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 5,
}

KINDS = ("grants", "discovery")

# Префікс — ідемпотентний ключ (_recent шукає по title LIKE 'префікс — %'),
# тому міняти його не можна без міграції даних у kb.briefs.
_TITLE_PREFIX = {
    "grants": "Weekly grants & RFPs",
    "discovery": "Weekly discovery",
}

# Питання веб-кроку. Контракт відповіді той самий, що в chat._web_research:
# «NONE» першим словом == нічого корисного == порожні findings.
_RESEARCH_PROMPTS = {
    "grants": (
        "Today is {today}. Search the web for Web3/EVM ecosystem grant "
        "programs, RFPs (requests for proposals) and foundation missions "
        "that are NEW or UPDATED within the last 7 days, plus ones with "
        "submission deadlines in the next 30 days. Cover the major EVM "
        "ecosystems (Ethereum, Optimism, Arbitrum, Base, Polygon, zkSync, "
        "Scroll, Linea, and similar L2s) AND newly launched EVM chains. "
        "For each: program name, ecosystem, budget/size, deadline, and the "
        "source URL."
    ),
    "discovery": (
        "Today is {today}. Search the web for, over the last 7 days: "
        "(1) new EVM-compatible chains or L2s announced or launched; "
        "(2) notable Web3 product launches and major protocol upgrades; "
        "(3) Web3/crypto funding rounds that closed — who raised, how "
        "much, lead investors. Include the source URL for every item."
    ),
}

_WEB_SYSTEM = (
    "Search the web for up-to-date facts answering the request. Reply with "
    "the findings and their source URLs, concisely. If the web has nothing "
    "useful, say exactly: NONE."
)

# Рендерер брифів у дашборді (admin.app.md_lite) знає ЛИШЕ `##`/`###` —
# `# ` лишається на екрані сирою решіткою (побачив на першому живому звіті
# 2026-08-28). Плюс заголовок першого рівня однаково дублював би title
# сторінки, який дашборд малює сам.
_FORMAT_RULE = (
    "Never open with a top-level \"# \" title: the dashboard prints the "
    "report title itself, and its renderer only understands \"##\" and "
    "\"###\" headers — a \"# \" line shows up as a literal hash. Start "
    "directly with the first \"##\" section."
)

# Синтез. Мова підставляється з settings.brief_language (той самий ключ, що
# читає briefing.make_brief — звіти й брифи мають говорити однією мовою).
_REPORT_SYSTEMS = {
    "grants": (
        "You write the Monday \"Weekly EVM grants & RFPs\" report for WOOF "
        "Software, a Web3 development agency deciding what to bid on this "
        "week. Write the report in {language}. Format: markdown (## "
        "headers, bullet lists, [title](url) links). Target under 700 "
        "words. Use exactly these sections:\n"
        "## New this week — new or updated grant programs, RFPs and "
        "foundation missions across EVM ecosystems, new chains included. "
        "Each item: name, ecosystem, size/budget if known, deadline, link, "
        "and one line on agency fit. An open competitive submission window "
        "matters more than retroactive or algorithmic funding.\n"
        "## Still open — deadlines approaching — carried-over "
        "opportunities with their dates.\n"
        "## Game plan — 3-5 concrete prioritized actions for the week: "
        "bid on X, prepare Y, watch Z.\n"
        "## Sources — numbered bare URLs.\n"
        "Grounding rules: use ONLY the web findings and the forum archive "
        "context provided below; never invent programs, amounts or "
        "deadlines; mark uncertain items \"(unverified)\". If the week is "
        "thin, say so honestly instead of inflating."
    ),
    "discovery": (
        "You write the Monday \"Weekly EVM discovery\" report for WOOF "
        "Software, a Web3 development agency looking for new ecosystems "
        "and clients. Write the report in {language}. Format: markdown "
        "(## headers, bullet lists, [title](url) links). Target under 700 "
        "words. Use exactly these sections:\n"
        "## New chains & products — new EVM chains/L2s, notable product "
        "launches, major protocol upgrades.\n"
        "## Funding & buying signals — who raised, how much, lead "
        "investors, and one line on why it is a buying signal (a freshly "
        "funded project is a potential client for the agency).\n"
        "## Forums worth adding — compare against the tracked-forums list "
        "in the context; for each untracked ecosystem worth following, "
        "give the governance forum URL, note whether it looks like a "
        "Discourse instance, and end the item with a ready-to-click line "
        "\"Add: https://rfpfetch.online/sources?url=<forum URL>\" (the "
        "dashboard pre-fills the add-source form from that link). Never "
        "propose a forum that is already tracked.\n"
        "## Sources — numbered bare URLs.\n"
        "Grounding rules: use ONLY the web findings and the archive "
        "context provided below; never invent facts; mark uncertain items "
        "\"(unverified)\". If the week is thin, say so honestly."
    ),
}

# Машинний блок дедлайнів — ЛИШЕ для grants-звіту: модель і так знаходить
# вікна подачі, тепер віддає їх структуровано; ми складаємо в kb.deadlines
# (міграція 015) для «Closing soon» на Overview і пінгів за 3 дні.
# Тільки ПІДТВЕРДЖЕНІ дати: (unverified) у таблицю дедлайнів не потрапляє —
# пінг «за 3 дні» по вигаданій даті гірший за відсутність пінга.
_DEADLINES_MARK = "---DEADLINES---"
_DEADLINES_RULE = (
    "Finally, after the Telegram digest, output a line containing exactly "
    + _DEADLINES_MARK + " and then a JSON array (nothing else after it) of "
    "the CONFIRMED submission deadlines mentioned in the report: "
    '[{"title": "...", "ecosystem": "...", "deadline": "YYYY-MM-DD", '
    '"url": "..."}]. Only include items whose deadline date you verified in '
    "the findings — never guessed or (unverified) ones. Output [] when "
    "there are none."
)

_KB_QUERIES = {
    "grants": (
        "request for proposals RFP",
        "grant program application",
        "funding proposal deadline",
    ),
    "discovery": (
        "mainnet launch",
        "raised funding round",
        "new protocol deployment",
    ),
}

_LANG_NAMES = {"en": "English", "uk": "Ukrainian"}

# Дайджест для Telegram пише САМА модель тим самим викликом, що й звіт
# (окремий рядок-маркер розділяє їх) — доплати немає, а якість незрівнянна
# з механічним обрізанням: перші 400 символів різали речення посеред слова
# й тягли в месенджер сиру розмітку. У групу йде дайджест + посилання на
# повний бріф (рішення Миколи 2026-08-28: «скорочені бріфи + посилання»).
_TELEGRAM_MARK = "---TELEGRAM---"
_TELEGRAM_RULE = (
    "After the report, output a line containing exactly " + _TELEGRAM_MARK +
    " and then a Telegram digest of the same report: plain text ONLY — no "
    "#, no **, no markdown links, no tables — at most 600 characters. "
    "Give 3-5 lines, each starting with \"- \", each naming one concrete "
    "item a reader must not miss (with the ecosystem and the deadline when "
    "there is one), then a final line \"Next: \" with the single most "
    "important action. It is read on a phone by people who may never open "
    "the full report, so it must stand on its own."
)

# Дайджест і так має прийти чистим, але модель зрідка лишає розмітку —
# у месенджері вона виглядає сміттям (правило Миколи: жодних ** і # у
# Telegram), тож чистимо детерміновано.
_MD_LINK_RE = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^\s()]+)\)")
_MD_HEAD_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_MD_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_MD_ITALIC_RE = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
_MD_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_BULLET_RE = re.compile(r"^\s*[-*]\s+", re.M)
_MD_RULE_RE = re.compile(r"^\s*-{3,}\s*$", re.M)


def _plain_text(md: str) -> str:
    """Markdown → чистий текст для месенджера. Посилання розгортаються в
    «підпис — URL»: у Telegram голий URL клікабельний сам, а «[текст](url)»
    лишився б сирим синтаксисом."""
    text = _MD_LINK_RE.sub(r"\1 — \2", md)
    text = _MD_RULE_RE.sub("", text)
    text = _MD_HEAD_RE.sub("", text)
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_ITALIC_RE.sub(r"\1", text)
    text = _MD_CODE_RE.sub(r"\1", text)
    text = _MD_BULLET_RE.sub("- ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _headline(bullet: str) -> str:
    """Перше речення пункту — воно й несе назву програми та екосистему;
    решта пункту це обґрунтування, яке в дайджест не влізе. Ріжемо по «. »,
    а не по N символах: обрізок посеред слова — те, за що дайджест і
    переробляли."""
    head, sep, _ = bullet.partition(". ")
    return (head + ".") if sep else bullet


def _fallback_digest(report_md: str, limit: int = 500) -> str:
    """Коли маркера немає (модель зігнорувала правило) або коли дайджест
    треба для вже збереженого звіту (гілка skipped: у kb.briefs лежить сам
    звіт, без дайджесту).

    Спершу — ПУНКТИ СПИСКУ: у цих звітах саме вони несуть конкретику, а
    перші рядки тексту це вступне застереження «тиждень тонкий» (перша
    жива перевірка 2026-08-28 віддала в Telegram саме його). Якщо пунктів
    нема — падаємо на послідовні рядки, теж по межах рядка.
    """
    text = _plain_text(report_md)
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    bullets: list[str] = []
    total = 0
    for line in lines:
        if not line.startswith("- "):
            continue
        head = _headline(line)
        if total + len(head) > limit:
            break
        bullets.append(head)
        total += len(head) + 1
    if len(bullets) >= 2:
        return "\n".join(bullets)

    out: list[str] = []
    total = 0
    for line in lines:
        if total + len(line) > limit:
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out) or text[:limit]

# Стеля контексту в символах — синтез і так отримує вижимку, а не сирі
# треди; без стелі 40 тем + 3 пошуки в поганий тиждень роздули б запит.
_MAX_CONTEXT_CHARS = 9000


def _db() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, client_encoding="utf8")


def _setting(conn: psycopg.Connection, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else default


def _recent(conn: psycopg.Connection, kind: str, today: str) -> dict | None:
    """Звіт цього виду ЗА СЬОГОДНІ, якщо є — ідемпотентний guard.

    Ключ — ПОВНИЙ заголовок із датою, а не вікно «останні N діб». Перша
    версія брала 3 доби і 2026-08-31 обпеклась: звіти, згенеровані вручну
    в п'ятницю 28-го, були «свіжішими за 3 доби», тож понеділковий крон
    (виконання 1156, 06:00 UTC) НЕ згенерував нічого — команда отримала в
    Telegram п'ятничні звіти під виглядом понеділкових. Вікно ширше за
    добу в принципі несумісне з тижневою каденцією: будь-який ручний
    прогін напередодні глушив би плановий. Ризик, заради якого guard і
    існує (подвійний постріл крону, повторний імпорт воркфлоу), живе в
    межах хвилин — доба покриває його з величезним запасом.
    """
    return conn.execute(
        "SELECT id, title, brief_md FROM kb.briefs "
        "WHERE title = %s ORDER BY id DESC LIMIT 1",
        (f"{_TITLE_PREFIX[kind]} — {today}",),
    ).fetchone()


def _web_findings(client, model: str, kind: str, today: str) -> tuple[str, int, int]:
    """Веб-крок. Ніколи не кидає: свіжий інтернет — бонус до архіву, а не
    умова звіту (той самий принцип, що й chat._web_research)."""
    try:
        response = client.messages.create(
            model=model,
            max_tokens=4000,
            system=[{"type": "text", "text": _WEB_SYSTEM}],
            tools=[_WEB_SEARCH_TOOL],
            messages=[{"role": "user",
                       "content": _RESEARCH_PROMPTS[kind].format(today=today)}],
        )
    except Exception:  # noqa: BLE001 — див. докстрінг
        log.exception("weekly %s: web research failed; continuing without it", kind)
        return "", 0, 0
    text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    if text.upper().startswith("NONE"):
        text = ""
    return (text,
            getattr(response.usage, "input_tokens", 0) or 0,
            getattr(response.usage, "output_tokens", 0) or 0)


def _kb_context(conn: psycopg.Connection, kind: str) -> str:
    """Детермінована вижимка з архіву — БЕЗ LLM: keyword-пошуки + теми,
    підняті за 7 днів (+ для discovery список відстежуваних форумів)."""
    parts: list[str] = []

    for query in _KB_QUERIES[kind]:
        result = kbtools.search_impl(query, limit=8)
        hits = result.get("hits") or []
        if not hits:
            continue
        parts.append(f"Archive search «{query}»:")
        for hit in hits:
            snippet = (hit.get("snippet") or "").replace("\n", " ")[:180]
            parts.append(
                f"- [{hit['forum']}] {hit['title']} — {hit.get('post_url') or ''}"
                + (f" :: {snippet}" if snippet else "")
            )

    recent = conn.execute(
        "SELECT title, forum_slug, url FROM kb.topics "
        "WHERE bumped_at > now() - interval '7 days' "
        "ORDER BY bumped_at DESC LIMIT 40"
    ).fetchall()
    if recent:
        parts.append("Forum topics active in the last 7 days:")
        parts.extend(f"- [{r['forum_slug']}] {r['title']} — {r['url']}" for r in recent)

    if kind == "discovery":
        forums = conn.execute(
            "SELECT forum_slug, base_url FROM kb.forums WHERE enabled "
            "ORDER BY forum_slug"
        ).fetchall()
        parts.append(
            "Tracked forums already in the knowledge base (do NOT propose "
            "these in «Forums worth adding»): "
            + ", ".join(f"{r['forum_slug']} ({r['base_url']})" for r in forums)
        )
        # Свідомо ВИКИНУТІ форуми (settings.weekly_forum_denylist). Їх нема
        # в списку вище САМЕ ТОМУ, що їх видалили — без цього рядка звіт
        # пропонував би їх щотижня як «нову можливість» (перший живий звіт
        # 2026-08-28 запропонував Lido, викинутий рішенням Миколи).
        denied = [s.strip() for s in
                  _setting(conn, "weekly_forum_denylist", "lido").split(",")
                  if s.strip()]
        if denied:
            parts.append(
                "Deliberately rejected — never propose these either, and do "
                "not explain why: " + ", ".join(denied)
            )

    return "\n".join(parts)[:_MAX_CONTEXT_CHARS]


def _synthesize(
    client, model: str, kind: str, language: str, today: str,
    web: str, kb_context: str,
) -> tuple[str, str, int, int]:
    """→ (звіт для kb.briefs, дайджест для Telegram, токени in, токени out)."""
    system = (_REPORT_SYSTEMS[kind].format(
        language=_LANG_NAMES.get(language, language)
    ) + "\n" + _FORMAT_RULE + "\n" + _TELEGRAM_RULE)
    if kind == "grants":
        system += "\n" + _DEADLINES_RULE
    user = (
        f"Today is {today}.\n\n"
        f"=== Web findings ===\n{web or '(web research unavailable this week)'}\n\n"
        f"=== Forum archive context ===\n{kb_context or '(no archive activity matched)'}"
    )
    response = client.messages.create(
        model=model,
        max_tokens=3000,
        system=[{"type": "text", "text": system}],
        messages=[{"role": "user", "content": user}],
    )
    raw = "\n".join(b.text for b in response.content if b.type == "text").strip()
    if not raw:
        raise RuntimeError("synthesis returned no text")
    # Службові блоки відрізаються ТУТ і в kb.briefs не потрапляють: дайджест
    # був би дублем власного змісту бріфа, а JSON дедлайнів — сміттям у
    # тексті. Ріжемо СПОЧАТКУ дедлайни (вони останні за правилом, але
    # partition по кожному маркеру окремо тримається, навіть якщо модель
    # переплутає порядок).
    raw, _, deadlines_json = raw.partition(_DEADLINES_MARK)
    report, _, digest = raw.partition(_TELEGRAM_MARK)
    report = report.strip()
    digest = _plain_text(digest)[:900] or _fallback_digest(report)
    return (report, digest, _parse_deadlines(deadlines_json),
            getattr(response.usage, "input_tokens", 0) or 0,
            getattr(response.usage, "output_tokens", 0) or 0)


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _parse_deadlines(block: str) -> list[dict]:
    """JSON-блок від моделі → чисті рядки для kb.deadlines. Ніколи не кидає:
    дедлайни — бонус до звіту, а не його умова; крива відповідь моделі не
    має валити вже синтезований звіт."""
    block = block.strip()
    if not block:
        return []
    # Модель інколи загортає JSON у ```-огорожу — знімаємо.
    block = re.sub(r"^```(?:json)?\s*|\s*```$", "", block)
    try:
        items = json.loads(block)
    except ValueError:
        log.warning("weekly: deadlines block is not valid JSON — dropped")
        return []
    if not isinstance(items, list):
        return []
    rows: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()[:300]
        deadline = str(it.get("deadline") or "").strip()
        if not title or not _DATE_RE.fullmatch(deadline):
            continue
        rows.append({
            "title": title,
            "ecosystem": str(it.get("ecosystem") or "").strip()[:80],
            "deadline": deadline,
            "url": str(it.get("url") or "").strip()[:500],
        })
    return rows


def _store_deadlines(conn, rows: list[dict]) -> int:
    """Upsert у kb.deadlines по (title, deadline) — той самий звітний рядок
    наступного тижня не плодить дублікат, а нове вікно тієї ж програми —
    легітимний новий рядок. dismissed_at НЕ чіпаємо: «прибрав з очей»
    людина, і повторна згадка у звіті не має повертати рядок на Overview."""
    for r in rows:
        conn.execute(
            """
            INSERT INTO kb.deadlines (title, ecosystem, deadline, url)
            VALUES (%(title)s, %(ecosystem)s, %(deadline)s, %(url)s)
            ON CONFLICT (title, deadline) DO UPDATE
                SET ecosystem = EXCLUDED.ecosystem, url = EXCLUDED.url
            """,
            r,
        )
    return len(rows)


# TTL magic-лінка: тиждень — рівно каденція звітів; давніший лінк у групі
# і так перекритий свіжішим повідомленням.
_SHARE_TTL_SECONDS = 7 * 24 * 3600


def share_token(brief_id: int) -> str:
    """Підписаний токен read-only перегляду ОДНОГО бріфа без логіну
    (magic-лінк у Telegram — UX п.2 плану 2026-08-31).

    Ключ — KB_MCP_TOKEN: він уже є і в kbmcp (тут), і в admin (env
    KB_MCP_TOKEN у compose), тож окремого спільного секрету не треба.
    Формат: "<expiry_unix>.<hmac_sha256(brief_id|expiry)[:32]>" — stdlib,
    без itsdangerous (його немає в образі kbmcp). Порожній KB_MCP_TOKEN →
    порожній токен (fail-closed: без секрета лінк не підписати, а generate
    без токена і так не працює)."""
    secret = os.environ.get("KB_MCP_TOKEN", "")
    if not secret:
        return ""
    expiry = int(time.time()) + _SHARE_TTL_SECONDS
    sig = hmac.new(secret.encode(), f"{brief_id}|{expiry}".encode(),
                   hashlib.sha256).hexdigest()[:32]
    return f"{expiry}.{sig}"


def generate(payload: dict) -> tuple[dict, int]:
    """→ (json-відповідь, HTTP-статус) — той самий контракт, що chat.answer.

    Порядок перевірок: токен (fail-closed) → kind → ідемпотентний guard →
    ключ Anthropic. Guard ДО перевірки ключа свідомо: «звіт уже є» —
    корисніша відповідь за 503 навіть на деплої без ключа.
    """
    # Fail-closed, як chat.answer: без KB_MCP_TOKEN Bearer-middleware
    # server.py вимкнений повністю, і цей маршрут стояв би голим у мережі.
    if not os.environ.get("KB_MCP_TOKEN", ""):
        return {"ok": False,
                "error": "Weekly reports are disabled: KB_MCP_TOKEN is not set."}, 403

    kind = (payload.get("kind") or "").strip()
    if kind not in KINDS:
        return {"ok": False,
                "error": f"kind must be one of: {', '.join(KINDS)}"}, 422

    force = bool(payload.get("force"))
    # Дата рахується ОДИН раз на виклик: ідемпотентний guard і заголовок
    # мусять бачити той самий день, інакше прогін через опівніч перевірив
    # би вчорашній ключ, а записав сьогоднішній.
    today = date.today().isoformat()

    with _db() as conn:
        if not force:
            recent = _recent(conn, kind, today)
            if recent:
                # Дайджест і тут — щоб повторний прогін ніс у Telegram суть,
                # а не саме лише «звіт уже є» з голим посиланням.
                return {"ok": True, "skipped": True,
                        "brief_id": recent["id"], "title": recent["title"],
                        "summary": _fallback_digest(recent["brief_md"]),
                        "share_token": share_token(recent["id"])}, 200

        if not ANTHROPIC_API_KEY:
            # Без stub-тира свідомо (див. докстрінг модуля): звалище сирих
            # сніпетів під виглядом тижневого звіту гірше за чесну відмову.
            return {"ok": False,
                    "error": "Weekly reports need ANTHROPIC_API_KEY: there is "
                             "no useful keyword-only tier for a weekly digest."}, 503

        model = _setting(conn, "brief_model", "claude-opus-5")
        override = payload.get("model")
        if isinstance(override, str) and _MODEL_RE.fullmatch(override):
            model = override
        language = _setting(conn, "brief_language", "en")
        kb_context = _kb_context(conn, kind)

    import anthropic  # ліниво, як briefing.py — stub-деплоям SDK не потрібен

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    web, web_in, web_out = _web_findings(client, model, kind, today)
    try:
        report, digest, deadlines, syn_in, syn_out = _synthesize(
            client, model, kind, language, today, web, kb_context)
    except Exception:  # noqa: BLE001 — n8n покаже чесний текст у Telegram
        log.exception("weekly %s: synthesis failed", kind)
        return {"ok": False,
                "error": "Report synthesis failed — see kbmcp logs."}, 502

    title = f"{_TITLE_PREFIX[kind]} — {today}"
    with _db() as conn:
        if deadlines:
            _store_deadlines(conn, deadlines)
        row = conn.execute(
            """
            INSERT INTO kb.briefs (item_uid, ecosystem, title, brief_md, tier,
                                   model, tokens_in, tokens_out)
            VALUES (NULL, 'EVM', %s, %s, 'llm', %s, %s, %s)
            RETURNING id
            """,
            (title, report, model, web_in + syn_in, web_out + syn_out),
        ).fetchone()
        conn.commit()

    log.info("weekly %s: brief %s, %s in / %s out tokens",
             kind, row["id"], web_in + syn_in, web_out + syn_out)
    return {"ok": True, "skipped": False, "brief_id": row["id"],
            "title": title, "summary": digest,
            "share_token": share_token(row["id"])}, 200
