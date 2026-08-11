"""Briefing packs — turn the forum archive into per-lead sales context.

Two tiers, chosen by whether ANTHROPIC_API_KEY is configured:

  basic  deterministic SQL over the archive: similar threads, most active
         voices, rejection discussions. Zero cost, zero dependencies —
         this is also the grounding context the LLM tier starts from.
  llm    an agentic loop: Claude gets kb_search/kb_topic tools scoped to the
         ecosystem's forum, investigates like an analyst, and writes a short
         cited brief. Model comes from settings.brief_model (admin-editable,
         no redeploy).

The brief must never block lead delivery: callers (n8n node, admin button)
invoke it AFTER seen_items.status='done' is written, with failures tolerated.
"""

from __future__ import annotations

import json
import logging
import os
import re

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger("kb-brief")

# Дозволена форма model-override (див. make_brief): будь-який claude-* id —
# admin шле лише значення зі свого select-а, але контракт відкритий і для
# n8n, тож форму перевіряємо тут, а невалідну мовчки ігноруємо.
_MODEL_RE = re.compile(r"claude-[a-z0-9.-]{3,40}")

DATABASE_URL = os.environ["DATABASE_URL"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

MAX_TOOL_ITERATIONS = 8       # the analyst loop is useful, not open-ended
SNIPPET_OPTIONS = "MaxWords=40, MinWords=15, StartSel=«, StopSel=»"

_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "into", "over",
    "proposal", "request", "rfp", "grant", "grants", "program", "update",
}


def _db() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, client_encoding="utf8")


def _setting(conn: psycopg.Connection, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else default


def _brief_max_words(conn: psycopg.Connection) -> int:
    """011: admin "depth slider", shared with chat.py's chat_brief (which
    keeps its own copy of this clamp — see that module's _brief_max_words for
    why it's duplicated rather than imported). Clamp defensively: a blank or
    non-numeric setting must fall back to the old hardcoded 350, never 500
    the brief over something this inconsequential."""
    raw = _setting(conn, "brief_max_words", "350")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 350
    return max(100, min(2000, n))


def _forum_for(conn: psycopg.Connection, ecosystem: str) -> dict | None:
    return conn.execute(
        """
        SELECT forum_slug, base_url FROM kb.forums
         WHERE lower(coalesce(ecosystem, '')) = lower(%s)
            OR forum_slug = lower(%s)
         -- 008: та сама екосистема тепер має і Discourse-, і snap-* рядок.
         -- Бриф ґрунтується на форумних ДИСКУСІЯХ (голоси/аргументи), а не на
         -- тілах пропозицій — Discourse свідомо першим; LIMIT 1 без цього
         -- ORDER BY вибирав би рядок недетерміновано.
         ORDER BY (kind = 'discourse') DESC
         LIMIT 1
        """,
        (ecosystem, ecosystem),
    ).fetchone()


def _title_query(title: str) -> str:
    """OR-query from the title's significant words — recall over precision."""
    words = [w for w in re.findall(r"[a-zA-Z]{4,}", title.lower()) if w not in _STOPWORDS]
    return " OR ".join(dict.fromkeys(words[:8])) or title[:60]


# ── Deterministic context (basic tier + LLM grounding) ─────────────


def _gather_context(conn: psycopg.Connection, forum_slug: str, title: str) -> dict:
    query = _title_query(title)

    similar = conn.execute(
        """
        SELECT DISTINCT ON (t.id) t.title, t.url, t.category_name, t.bumped_at,
               t.post_count,
               ts_rank_cd(p.body_tsv, websearch_to_tsquery('english', %(q)s)) AS rank
          FROM kb.posts p JOIN kb.topics t ON t.id = p.topic_ref
         WHERE t.forum_slug = %(f)s
           AND p.body_tsv @@ websearch_to_tsquery('english', %(q)s)
         ORDER BY t.id, rank DESC
         LIMIT 12
        """,
        {"q": query, "f": forum_slug},
    ).fetchall()
    similar.sort(key=lambda r: r["rank"], reverse=True)
    similar = similar[:8]

    voices = conn.execute(
        """
        SELECT p.author, count(*) AS posts, max(p.posted_at) AS last_seen
          FROM kb.posts p JOIN kb.topics t ON t.id = p.topic_ref
         WHERE t.forum_slug = %(f)s
           AND p.posted_at > now() - interval '180 days'
           AND p.author IS NOT NULL
         GROUP BY p.author ORDER BY posts DESC LIMIT 6
        """,
        {"f": forum_slug},
    ).fetchall()

    rejections = conn.execute(
        """
        SELECT DISTINCT ON (t.id) t.title, t.url,
               ts_headline('english', p.raw_text,
                           websearch_to_tsquery('english', %(q)s), %(opts)s) AS snippet
          FROM kb.posts p JOIN kb.topics t ON t.id = p.topic_ref
         WHERE t.forum_slug = %(f)s
           AND p.body_tsv @@ websearch_to_tsquery('english',
                 'rejected OR declined OR "voted against" OR "not fund"')
           AND t.title_tsv @@ websearch_to_tsquery('english', %(q)s)
         ORDER BY t.id LIMIT 5
        """,
        {"q": query, "f": forum_slug, "opts": SNIPPET_OPTIONS},
    ).fetchall()

    return {"query": query, "similar": similar, "voices": voices, "rejections": rejections}


def _basic_brief(ecosystem: str, title: str, ctx: dict) -> str:
    lines = [f"## KB brief: {ecosystem} — {title}", ""]

    lines.append("**Similar discussions in the governance forum:**")
    if ctx["similar"]:
        for r in ctx["similar"]:
            when = r["bumped_at"].strftime("%Y-%m") if r["bumped_at"] else "?"
            lines.append(
                f"- [{r['title']}]({r['url']}) — {r['category_name'] or 'uncategorized'}, "
                f"{r['post_count'] or '?'} posts, last activity {when}"
            )
    else:
        lines.append("- nothing closely related found in the archive")

    lines.append("")
    lines.append("**Most active forum voices (last 180 days):**")
    for v in ctx["voices"]:
        lines.append(f"- {v['author']} — {v['posts']} posts, last seen "
                     f"{v['last_seen'].strftime('%Y-%m-%d') if v['last_seen'] else '?'}")

    if ctx["rejections"]:
        lines.append("")
        lines.append("**Rejection/decline discussions touching this topic:**")
        for r in ctx["rejections"]:
            lines.append(f"- [{r['title']}]({r['url']}): {r['snippet']}")

    lines.append("")
    lines.append("_Auto-generated from the forum archive (keyword tier — set "
                 "ANTHROPIC_API_KEY for analyst-grade briefs)._")
    return "\n".join(lines)


# ── LLM tier — agentic loop over the archive ───────────────────────


def _tool_kb_search(conn: psycopg.Connection, forum_slug: str, query: str, limit: int = 10) -> str:
    rows = conn.execute(
        """
        SELECT t.topic_id, t.title, t.category_name,
               t.url || '/' || p.post_number AS post_url, p.author, p.posted_at,
               ts_headline('english', p.raw_text,
                           websearch_to_tsquery('english', %(q)s), %(opts)s) AS snippet
          FROM kb.posts p JOIN kb.topics t ON t.id = p.topic_ref
         WHERE t.forum_slug = %(f)s
           AND p.body_tsv @@ websearch_to_tsquery('english', %(q)s)
         ORDER BY ts_rank_cd(p.body_tsv, websearch_to_tsquery('english', %(q)s)) DESC
         LIMIT %(n)s
        """,
        {"q": query, "f": forum_slug, "n": max(1, min(int(limit), 20)), "opts": SNIPPET_OPTIONS},
    ).fetchall()
    return json.dumps([
        {
            "topic_id": r["topic_id"], "title": r["title"],
            "category": r["category_name"], "post_url": r["post_url"],
            "author": r["author"],
            "date": r["posted_at"].isoformat()[:10] if r["posted_at"] else None,
            "snippet": r["snippet"],
        }
        for r in rows
    ], ensure_ascii=False)


def _tool_kb_topic(conn: psycopg.Connection, forum_slug: str, topic_id: int, offset: int = 0) -> str:
    topic = conn.execute(
        "SELECT * FROM kb.topics WHERE forum_slug = %s AND topic_id = %s",
        (forum_slug, topic_id),
    ).fetchone()
    if not topic:
        return json.dumps({"error": f"topic {topic_id} not in archive"})
    posts = conn.execute(
        """
        SELECT post_number, author, posted_at, left(raw_text, 2500) AS text
          FROM kb.posts WHERE topic_ref = %s
         ORDER BY post_number OFFSET %s LIMIT 30
        """,
        (topic["id"], max(0, int(offset))),
    ).fetchall()
    return json.dumps({
        "title": topic["title"], "url": topic["url"],
        "post_count": topic["post_count"],
        "posts": [
            {"n": p["post_number"], "author": p["author"],
             "date": p["posted_at"].isoformat()[:10] if p["posted_at"] else None,
             "text": p["text"], "cite": f"{topic['url']}/{p['post_number']}"}
            for p in posts
        ],
    }, ensure_ascii=False)


_TOOLS = [
    {
        "name": "kb_search",
        "description": (
            "Full-text search over this ecosystem's governance forum archive. "
            "Call it with 2-4 differently-phrased queries (forum vocabulary "
            "matters: RetroPGF, mission, RFP, service provider, temp check)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "kb_topic",
        "description": "Read a full thread by topic_id (from kb_search results) before citing it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic_id": {"type": "integer"},
                "offset": {"type": "integer"},
            },
            "required": ["topic_id"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]

# {max_words}: filled in per-call from settings.brief_max_words (011, admin
# "depth slider" — see _brief_max_words) via str.format in _llm_brief. Cache
# tradeoff: this text is the cache_control ephemeral system block below, and
# Anthropic's prompt cache matches on exact byte content — parameterizing it
# means the cache is invalidated only when someone actually moves the slider
# (rare), and stays a hit across the many identical-setting calls in between.
# Cheaper than the alternative (a fixed prompt + a separate word-count
# instruction appended outside the cached block) would have been to reason
# about, for a setting that changes on the order of "never".
_SYSTEM = """You are a bid-research analyst for a Web3 development agency.
A new sales lead just arrived; your job is a briefing pack the salesperson
reads in 60 seconds before opening the conversation.

Work like an analyst: search the forum archive with several query phrasings,
read the threads that matter (kb_topic), then write the brief.

Rules:
- Every factual claim about the ecosystem MUST carry a markdown link to the
  specific forum post that supports it. No citation → drop the claim.
- State facts only from the archive. If the archive is thin on something,
  say "no data in archive" rather than guessing.
- Keep it under {max_words} words. Structure:
  ### What they fund / discuss (similar work)
  ### Who decides & active voices
  ### Risks: rejected or contested proposals
  ### Suggested angle (1-2 sentences, grounded in the above)
- Keep responses focused and concise; skip non-essential context. Write for
  a salesperson, not a researcher: plain language, no forum jargon without a
  one-word gloss."""


def _llm_brief(
    conn: psycopg.Connection,
    forum_slug: str,
    ecosystem: str,
    title: str,
    body: str,
    model: str,
    language: str,
    max_words: int,
) -> tuple[str, int, int]:
    """Manual tool-use loop (bounded, no beta dependency). Returns (md, in, out)."""
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    ctx = _gather_context(conn, forum_slug, title)
    grounding = _basic_brief(ecosystem, title, ctx)

    language_note = (
        "" if language == "en"
        else f"\nWrite the brief in language code: {language}."
    )
    messages = [{
        "role": "user",
        "content": (
            f"LEAD:\nEcosystem: {ecosystem}\nTitle: {title}\n"
            f"Details:\n{(body or '')[:3000]}\n\n"
            f"Keyword pre-scan of the archive (verify before trusting):\n{grounding}"
            f"{language_note}"
        ),
    }]

    system_text = _SYSTEM.format(max_words=max_words)

    tokens_in = tokens_out = 0
    for _ in range(MAX_TOOL_ITERATIONS):
        response = client.messages.create(
            model=model,
            max_tokens=4000,
            system=[{"type": "text", "text": system_text,
                     "cache_control": {"type": "ephemeral"}}],
            tools=_TOOLS,
            messages=messages,
        )
        tokens_in += response.usage.input_tokens
        tokens_out += response.usage.output_tokens

        if response.stop_reason == "refusal":
            log.warning("brief refused for %s; falling back to basic", ecosystem)
            return grounding, tokens_in, tokens_out

        if response.stop_reason != "tool_use":
            text = "\n".join(b.text for b in response.content if b.type == "text")
            return (text or grounding), tokens_in, tokens_out

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            try:
                if block.name == "kb_search":
                    payload = _tool_kb_search(conn, forum_slug, **block.input)
                elif block.name == "kb_topic":
                    payload = _tool_kb_topic(conn, forum_slug, **block.input)
                else:
                    payload = json.dumps({"error": f"unknown tool {block.name}"})
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": payload})
            except Exception as exc:  # noqa: BLE001 — feed the error back to the model
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": f"tool error: {exc}", "is_error": True})
        messages.append({"role": "user", "content": results})

    log.warning("brief loop hit iteration cap for %s; using basic tier", ecosystem)
    return _basic_brief(ecosystem, title, _gather_context(conn, forum_slug, title)), \
        tokens_in, tokens_out


# ── Entry point ────────────────────────────────────────────────────


def make_brief(
    ecosystem: str,
    title: str,
    body: str = "",
    item_uid: str | None = None,
    model_override: str | None = None,
) -> dict:
    """Generate (and persist) a briefing pack. Never raises on LLM problems —
    degrades to the basic tier; raises only if the ecosystem has no archive.

    `model_override` — вибір моделі на ОДИН виклик (запит Миколи 2026-08-11:
    «вибирати глибину бріфа при його створенні»): admin шле значення зі
    свого select-а. Валідовано формою claude-* — невалідне мовчки падає на
    settings.brief_model, а не 500-ить (кривий override не вартий зламаного
    бріфа)."""
    with _db() as conn:
        forum = _forum_for(conn, ecosystem)
        if not forum:
            archived = [r["forum_slug"] for r in conn.execute(
                "SELECT forum_slug FROM kb.forums WHERE ecosystem IS NOT NULL")]
            return {
                "error": f"no archived forum for ecosystem {ecosystem!r}",
                "archived_ecosystems": archived,
            }

        model = _setting(conn, "brief_model", "claude-opus-5")
        if model_override and _MODEL_RE.fullmatch(model_override):
            model = model_override
        language = _setting(conn, "brief_language", "en")
        max_words = _brief_max_words(conn)
        slug = forum["forum_slug"]

        tier, tokens_in, tokens_out = "basic", None, None
        if ANTHROPIC_API_KEY:
            try:
                brief_md, tokens_in, tokens_out = _llm_brief(
                    conn, slug, ecosystem, title, body, model, language, max_words
                )
                tier = "llm"
            except Exception:  # noqa: BLE001 — API down ≠ no brief
                log.exception("llm brief failed; degrading to basic tier")
                brief_md = _basic_brief(ecosystem, title, _gather_context(conn, slug, title))
        else:
            brief_md = _basic_brief(ecosystem, title, _gather_context(conn, slug, title))

        row = conn.execute(
            """
            INSERT INTO kb.briefs (item_uid, ecosystem, title, brief_md, tier,
                                   model, tokens_in, tokens_out)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, created_at
            """,
            (item_uid, ecosystem, title, brief_md, tier,
             model if tier == "llm" else None, tokens_in, tokens_out),
        ).fetchone()
        conn.commit()

    return {
        "brief_id": row["id"],
        "tier": tier,
        "model": model if tier == "llm" else None,
        "brief_md": brief_md,
        "tokens": {"in": tokens_in, "out": tokens_out},
    }
