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

import logging
import os
import re
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
        "Discourse instance, and remind that it can be added on the "
        "dashboard via Sources → Detect type → Discover categories. Never "
        "propose a forum that is already tracked.\n"
        "## Sources — numbered bare URLs.\n"
        "Grounding rules: use ONLY the web findings and the archive "
        "context provided below; never invent facts; mark uncertain items "
        "\"(unverified)\". If the week is thin, say so honestly."
    ),
}

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

# Стеля контексту в символах — синтез і так отримує вижимку, а не сирі
# треди; без стелі 40 тем + 3 пошуки в поганий тиждень роздули б запит.
_MAX_CONTEXT_CHARS = 9000


def _db() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, client_encoding="utf8")


def _setting(conn: psycopg.Connection, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else default


def _recent(conn: psycopg.Connection, kind: str) -> dict | None:
    """Звіт цього виду за останні 3 доби, якщо є — ідемпотентний guard."""
    return conn.execute(
        "SELECT id, title FROM kb.briefs "
        "WHERE title LIKE %s AND created_at > now() - interval '3 days' "
        "ORDER BY id DESC LIMIT 1",
        (_TITLE_PREFIX[kind] + " — %",),
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
) -> tuple[str, int, int]:
    system = _REPORT_SYSTEMS[kind].format(
        language=_LANG_NAMES.get(language, language)
    ) + "\n" + _FORMAT_RULE
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
    report = "\n".join(b.text for b in response.content if b.type == "text").strip()
    if not report:
        raise RuntimeError("synthesis returned no text")
    return (report,
            getattr(response.usage, "input_tokens", 0) or 0,
            getattr(response.usage, "output_tokens", 0) or 0)


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

    with _db() as conn:
        if not force:
            recent = _recent(conn, kind)
            if recent:
                return {"ok": True, "skipped": True,
                        "brief_id": recent["id"], "title": recent["title"]}, 200

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
    today = date.today().isoformat()

    web, web_in, web_out = _web_findings(client, model, kind, today)
    try:
        report, syn_in, syn_out = _synthesize(
            client, model, kind, language, today, web, kb_context)
    except Exception:  # noqa: BLE001 — n8n покаже чесний текст у Telegram
        log.exception("weekly %s: synthesis failed", kind)
        return {"ok": False,
                "error": "Report synthesis failed — see kbmcp logs."}, 502

    title = f"{_TITLE_PREFIX[kind]} — {today}"
    with _db() as conn:
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
            "title": title, "summary": report[:400]}, 200
