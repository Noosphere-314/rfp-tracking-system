"""Пошукове ядро KB — SQL за search_kb/get_topic, винесений з server.py.

Причина винесення: тепер це ядро потрібне ДВОМ викликачам — MCP-інструментам
server.py (Claude Code/Desktop) і чат-агенту chat.py (веб/телеграм-запитання
команди). Тримати один і той самий SQL у двох файлах — значить рано чи пізно
розсинхронізувати їх; тут він один, а server.py лишає собі тонкі @mcp.tool
обгортки (їхні докстрінги — це і є описи інструментів для Claude, тому вони
лишаються там).

_db() і _log() теж переїхали сюди з тієї ж причини: chat.py логує виклики
search_kb/get_topic у kb.query_log так само, як і MCP-інструменти, і не
повинен для цього тягнути окрему копію коду. server.py імпортує обидві назад
— вони й досі потрібні йому напряму (live_forum_search, /health).

Кожна функція відкриває власне коротке з'єднання (як і було в server.py) —
жоден виклик не тримає conn довше, ніж потрібно для його власних запитів.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger("kb-mcp")

DATABASE_URL = os.environ["DATABASE_URL"]


def _db() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, client_encoding="utf8")


def _log(tool: str, query: str | None, forum: str | None, hits: int) -> None:
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO kb.query_log (tool, query, forum_slug, hits) "
                "VALUES (%s, %s, %s, %s)",
                (tool, query, forum, hits),
            )
            conn.commit()
    except Exception:  # noqa: BLE001 — instrumentation must never break answers
        log.exception("query_log insert failed")


def search_impl(
    query: str,
    forum: str | None = None,
    category: str | None = None,
    after: str | None = None,
    limit: int = 20,
) -> dict:
    """SQL core of search_kb. See server.py's @mcp.tool search_kb for the
    public tool contract (that docstring is what Claude reads)."""
    limit = max(1, min(int(limit), 50))
    after_ts = None
    if after:
        try:
            after_ts = datetime.fromisoformat(after)
            if after_ts.tzinfo is None:
                after_ts = after_ts.replace(tzinfo=timezone.utc)
        except ValueError:
            return {"error": f"`after` must be an ISO date (got {after!r})"}

    sql = """
        SELECT t.forum_slug, t.topic_id, t.title, t.category_name,
               t.url || '/' || p.post_number AS post_url,
               p.post_number, p.author, p.posted_at,
               ts_rank_cd(p.body_tsv, q) AS rank,
               ts_headline('english', p.raw_text, q,
                           'MaxWords=45, MinWords=20, MaxFragments=2, '
                           'StartSel=«, StopSel=»') AS snippet
          FROM kb.posts p
          JOIN kb.topics t ON t.id = p.topic_ref,
               websearch_to_tsquery('english', %(query)s) q
         WHERE p.body_tsv @@ q
           AND (%(forum)s::text IS NULL OR t.forum_slug = %(forum)s)
           AND (%(category)s::text IS NULL
                OR t.category_name ILIKE '%%' || %(category)s || '%%')
           AND (%(after)s::timestamptz IS NULL OR p.posted_at >= %(after)s)
         ORDER BY rank DESC, p.posted_at DESC
         LIMIT %(limit)s
    """
    params = {
        "query": query, "forum": forum, "category": category,
        "after": after_ts, "limit": limit,
    }
    with _db() as conn:
        rows = conn.execute(sql, params).fetchall()
        # Title-only hits (the topic matches but the phrase never appears in a body).
        title_rows = conn.execute(
            """
            SELECT t.forum_slug, t.topic_id, t.title, t.category_name, t.url,
                   t.bumped_at, t.post_count
              FROM kb.topics t, websearch_to_tsquery('english', %(query)s) q
             WHERE t.title_tsv @@ q
               AND (%(forum)s::text IS NULL OR t.forum_slug = %(forum)s)
             ORDER BY t.bumped_at DESC NULLS LAST LIMIT 10
            """,
            {"query": query, "forum": forum},
        ).fetchall()

    hits = [
        {
            "forum": r["forum_slug"], "topic_id": r["topic_id"],
            "title": r["title"], "category": r["category_name"],
            "post_url": r["post_url"], "post_number": r["post_number"],
            "author": r["author"],
            "posted_at": r["posted_at"].isoformat() if r["posted_at"] else None,
            "snippet": r["snippet"],
        }
        for r in rows
    ]
    topic_hits = [
        {
            "forum": r["forum_slug"], "topic_id": r["topic_id"],
            "title": r["title"], "category": r["category_name"], "url": r["url"],
            "post_count": r["post_count"],
        }
        for r in title_rows
    ]
    _log("search_kb", query, forum, len(hits))
    return {
        "post_hits": hits,
        "topic_title_hits": topic_hits,
        "hint": (
            "No hits? Reword with forum vocabulary (RetroPGF, mission, ARFC, "
            "temp check) or drop the forum filter."
            if not hits and not topic_hits else
            "Read full threads with get_topic(forum, topic_id) before citing."
        ),
    }


def findings_impl(
    ecosystem: str | None = None,
    status: str | None = None,
    days: int = 14,
    min_confidence: float | None = None,
    limit: int = 10,
) -> dict:
    """SQL core of the chat agent's list_findings tool (agent 2.0 — internal
    pipeline data, NOT the kb.* forum archive: seen_items is our own
    findings/leads with classifier verdicts, joined to sources for
    ecosystem). Own short connection, no kb.query_log entry — that log is
    scoped to archive tools (search_kb/get_topic/live_forum_search), and
    seen_items/sources live outside the kb schema entirely.

    Fully-qualifies public.seen_items/public.sources rather than relying on
    the app role's default search_path (see mcp/chat.py:178-181 for why that
    default is currently safe) — kbtools.py's own convention is to always
    schema-qualify (kb.posts, kb.topics, kb.query_log), so new tables here
    follow the same habit rather than being the one exception.
    """
    days = max(1, min(int(days), 90))
    limit = max(1, min(int(limit), 20))
    sql = """
        SELECT i.title, i.url, s.ecosystem, i.status, i.category, i.confidence,
               i.delivered_at, i.first_seen
          FROM public.seen_items i
          JOIN public.sources s ON s.id = i.source_id
         WHERE i.first_seen >= now() - (%(days)s * interval '1 day')
           AND (%(ecosystem)s::text IS NULL OR s.ecosystem = %(ecosystem)s)
           AND (%(status)s::text IS NULL OR i.status = %(status)s)
           AND (%(min_confidence)s::real IS NULL OR i.confidence >= %(min_confidence)s)
         ORDER BY i.first_seen DESC
         LIMIT %(limit)s
    """
    params = {
        "days": days, "ecosystem": ecosystem, "status": status,
        "min_confidence": min_confidence, "limit": limit,
    }
    with _db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {
        "findings": [
            {
                "title": r["title"],
                "url": r["url"],
                "ecosystem": r["ecosystem"],
                "status": r["status"],
                "category": r["category"],
                "confidence": r["confidence"],
                "delivered_at": r["delivered_at"].isoformat() if r["delivered_at"] else None,
                "first_seen": r["first_seen"].isoformat() if r["first_seen"] else None,
            }
            for r in rows
        ],
    }


def topic_impl(forum: str, topic_id: int, offset: int = 0, max_posts: int = 60) -> dict:
    """SQL core of get_topic. See server.py's @mcp.tool get_topic for the
    public tool contract (that docstring is what Claude reads)."""
    max_posts = max(1, min(int(max_posts), 200))
    offset = max(0, int(offset))
    with _db() as conn:
        topic = conn.execute(
            "SELECT * FROM kb.topics WHERE forum_slug = %s AND topic_id = %s",
            (forum, topic_id),
        ).fetchone()
        if not topic:
            _log("get_topic", str(topic_id), forum, 0)
            return {"error": f"topic {topic_id} not in the {forum} archive"}

        posts = conn.execute(
            """
            SELECT post_number, author, posted_at, raw_text
              FROM kb.posts WHERE topic_ref = %s
             ORDER BY post_number OFFSET %s LIMIT %s
            """,
            (topic["id"], int(offset), max_posts),
        ).fetchall()

    _log("get_topic", str(topic_id), forum, len(posts))
    return {
        "title": topic["title"],
        "url": topic["url"],
        "category": topic["category_name"],
        "created_at": topic["created_at"].isoformat() if topic["created_at"] else None,
        "post_count": topic["post_count"],
        "returned": len(posts),
        "offset": offset,
        "posts": [
            {
                "n": p["post_number"],
                "author": p["author"],
                "at": p["posted_at"].isoformat() if p["posted_at"] else None,
                "text": p["raw_text"],
                "cite": f"{topic['url']}/{p['post_number']}",
            }
            for p in posts
        ],
    }
