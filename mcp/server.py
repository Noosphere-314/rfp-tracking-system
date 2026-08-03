"""KB MCP server — Claude's window into the forum archive (KB-Module-Design §4).

Three tools, deliberately small: the intelligence lives in the calling agent
(Claude issues refined queries, reads whole threads, follows references), not
in the retrieval layer. Every call is logged to kb.query_log — that log is the
instrumentation that later decides the embeddings question (§7).

Auth: static bearer token (KB_MCP_TOKEN). Empty token = auth off, which is
acceptable only on localhost; the compose file binds it to 127.0.0.1.

Connect from Claude Code:
    claude mcp add rfp-kb --transport http http://localhost:8765/mcp \
        --header "Authorization: Bearer <KB_MCP_TOKEN>"
"""

from __future__ import annotations

import hmac
import logging
import os
from datetime import datetime, timezone

import httpx
import psycopg
from fastmcp import FastMCP
from psycopg.rows import dict_row
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger("kb-mcp")

DATABASE_URL = os.environ["DATABASE_URL"]
TOKEN = os.environ.get("KB_MCP_TOKEN", "")
USER_AGENT = os.environ.get(
    "USER_AGENT", "RFP-Tracker-KB/1.0 (+mailto:ops@example.com)"
)

mcp = FastMCP(
    "rfp-kb",
    instructions=(
        "Full-text archive of Web3 governance forums (Optimism, Arbitrum, …). "
        "Strategy: run search_kb with 3-5 phrasing variants of the question "
        "(synonyms matter: 'dev tooling' vs 'developer infrastructure'), read "
        "promising threads in full via get_topic, cite post URLs in answers. "
        "live_forum_search hits the live forum /search.json — use it sparingly, "
        "for questions about very recent activity the archive may not have yet."
    ),
)


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


@mcp.tool
def search_kb(
    query: str,
    forum: str | None = None,
    category: str | None = None,
    after: str | None = None,
    limit: int = 20,
) -> dict:
    """Full-text search over the forum archive. Returns ranked post snippets.

    Args:
        query: plain-language search (websearch syntax: quotes for phrases,
               OR, -exclusions all work)
        forum: restrict to one forum slug (e.g. 'optimism', 'arbitrum')
        category: substring filter on the topic's category name
        after: ISO date — only posts on/after it
        limit: max hits (default 20, cap 50)
    """
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


@mcp.tool
def get_topic(forum: str, topic_id: int, offset: int = 0, max_posts: int = 60) -> dict:
    """Read a full thread from the archive, in post order, with citation URLs.

    Args:
        forum: forum slug (e.g. 'optimism')
        topic_id: numeric Discourse topic id (from search_kb results)
        offset: skip this many posts (for very long threads)
        max_posts: how many posts to return (default 60, cap 200)
    """
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


@mcp.tool
def live_forum_search(forum: str, query: str) -> dict:
    """Search the LIVE forum via its own /search.json.

    Interactive use only — one polite request per call, never for bulk
    crawling (robots.txt forbids automated /search use). Reach for it when
    the question concerns the last hours/days, which the archive may lag on.
    """
    with _db() as conn:
        row = conn.execute(
            "SELECT base_url FROM kb.forums WHERE forum_slug = %s", (forum,)
        ).fetchone()
    if not row:
        return {"error": f"unknown forum slug {forum!r}"}

    try:
        response = httpx.get(
            f"{row['base_url'].rstrip('/')}/search.json",
            params={"q": query},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        _log("live_forum_search", query, forum, 0)
        return {"error": f"live search failed: {exc}"}

    try:
        payload = response.json()
    except ValueError:  # 200 with an HTML interstitial (Cloudflare)
        _log("live_forum_search", query, forum, 0)
        return {"error": "live search returned non-JSON (likely a bot check)"}
    topics = {t["id"]: t for t in payload.get("topics") or []}
    results = [
        {
            "topic_id": post.get("topic_id"),
            "title": topics.get(post.get("topic_id"), {}).get("title")
                     or post.get("topic_title_headline", ""),
            "url": f"{row['base_url'].rstrip('/')}/t/{post.get('topic_id')}/{post.get('post_number')}",
            "blurb": post.get("blurb", ""),
            "created_at": post.get("created_at"),
        }
        for post in (payload.get("posts") or [])[:20]
    ]
    _log("live_forum_search", query, forum, len(results))
    return {"results": results}


@mcp.tool
def brief_for_lead(ecosystem: str, title: str, body: str = "") -> dict:
    """Generate a sales briefing pack for a lead from the forum archive:
    similar funded work, decision makers, rejected proposals, suggested angle.

    Args:
        ecosystem: e.g. 'Optimism', 'Arbitrum' — must have an archived forum
        title: the lead/opportunity title
        body: optional lead details for better targeting
    """
    import briefing

    result = briefing.make_brief(ecosystem, title, body)
    _log("brief_for_lead", title, ecosystem, 0 if result.get("error") else 1)
    return result


@mcp.custom_route("/brief", methods=["POST"])
async def brief_route(request: Request) -> JSONResponse:
    """HTTP entry for n8n / admin. Bearer-protected by the middleware.

    Called AFTER the lead is marked done — a brief failure must never affect
    delivery, so callers set continue-on-fail and this route never 500s on
    LLM problems (briefing degrades internally)."""
    from starlette.concurrency import run_in_threadpool

    import briefing

    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=422)

    ecosystem = (payload.get("ecosystem") or "").strip()
    title = (payload.get("title") or "").strip()
    if not ecosystem or not title:
        return JSONResponse({"error": "ecosystem and title are required"}, status_code=422)

    result = await run_in_threadpool(
        briefing.make_brief,
        ecosystem, title,
        payload.get("body") or "",
        payload.get("item_uid"),
    )
    _log("brief_for_lead", title, ecosystem, 0 if result.get("error") else 1)
    status = 404 if result.get("error") else 200
    return JSONResponse(result, status_code=status)


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    from starlette.concurrency import run_in_threadpool

    def counts() -> dict:
        with _db() as conn:
            return conn.execute(
                "SELECT count(DISTINCT t.id) AS topics, count(p.id) AS posts "
                "FROM kb.topics t LEFT JOIN kb.posts p ON p.topic_ref = t.id"
            ).fetchone()

    return JSONResponse({"ok": True, **await run_in_threadpool(counts)})


def build_app():
    """ASGI app with static-bearer middleware (auth off when no token set)."""
    app = mcp.http_app()

    if TOKEN:
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import Response

        class Bearer(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if request.url.path == "/health":
                    return await call_next(request)
                supplied = request.headers.get("Authorization", "")
                if not hmac.compare_digest(supplied, f"Bearer {TOKEN}"):
                    return Response("unauthorized", status_code=401)
                return await call_next(request)

        app.add_middleware(Bearer)
    else:
        log.warning("KB_MCP_TOKEN unset — auth disabled; acceptable on localhost only")
    return app


app = build_app()

if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8000)
