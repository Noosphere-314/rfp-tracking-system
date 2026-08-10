"""KB MCP server — Claude's window into the forum archive (KB-Module-Design §4).

Three tools, deliberately small: the intelligence lives in the calling agent
(Claude issues refined queries, reads whole threads, follows references), not
in the retrieval layer. Every call is logged to kb.query_log — that log is the
instrumentation that later decides the embeddings question (§7).

Auth: static bearer token (KB_MCP_TOKEN). Empty token = auth off, which is
acceptable only on localhost; the compose file binds it to 127.0.0.1.

/chat (chat.py) is the one HTTP route that does NOT tolerate an empty token
the way the rest of this file does: an unset KB_MCP_TOKEN turns off the
Bearer middleware below entirely, which would otherwise leave the chat agent
(and its LLM spend) open to anyone who can reach the port. See chat.answer's
fail-closed check.

Connect from Claude Code:
    claude mcp add rfp-kb --transport http http://localhost:8765/mcp \
        --header "Authorization: Bearer <KB_MCP_TOKEN>"
"""

from __future__ import annotations

import hmac
import logging
import os

import httpx

from netguard import SsrfBlocked, assert_public_url
from kbtools import _db, _log, search_impl, topic_impl
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger("kb-mcp")

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
    # SQL живе в kbtools.py — той самий search_impl обслуговує і чат-агента
    # (mcp/chat.py). Ця обгортка лишається лише заради докстрінга вище: він
    # і є описом інструмента, який читає Claude.
    return search_impl(query, forum=forum, category=category, after=after, limit=limit)


@mcp.tool
def get_topic(forum: str, topic_id: int | str, offset: int = 0, max_posts: int = 60) -> dict:
    """Read a full thread from the archive, in post order, with citation URLs.

    Args:
        forum: forum slug (e.g. 'optimism')
        topic_id: numeric Discourse topic id (from search_kb results)
        offset: skip this many posts (for very long threads)
        max_posts: how many posts to return (default 60, cap 200)
    """
    return topic_impl(forum, topic_id, offset=offset, max_posts=max_posts)


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

    # `base_url` приходить із kb.forums, куди його кладе форма в адмінці —
    # тобто це той самий недовірений вхід, що й у sources (worker/http.py).
    target = f"{row['base_url'].rstrip('/')}/search.json"
    try:
        assert_public_url(target)
    except SsrfBlocked as exc:
        _log("live_forum_search", query, forum, 0)
        return {"error": f"forum url rejected: {exc}"}

    try:
        response = httpx.get(
            target,
            params={"q": query},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
            # Редіректи веде assert_public_url вище лише для першого кроку;
            # 302 на внутрішній хост обійшов би перевірку, тож не ходимо за ними.
            follow_redirects=False,
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


@mcp.custom_route("/chat", methods=["POST"])
async def chat_route(request: Request) -> JSONResponse:
    """HTTP entry for the web dashboard and the Telegram bot. The Bearer
    middleware below already covers this path — but only when a token is
    configured; chat.answer adds its own fail-closed check for the token-unset
    case (see module docstring)."""
    from starlette.concurrency import run_in_threadpool

    import chat

    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    result, status = await run_in_threadpool(chat.answer, payload)
    return JSONResponse(result, status_code=status)


@mcp.custom_route("/keywords-advice", methods=["POST"])
async def keywords_advice_route(_request: Request) -> JSONResponse:
    """HTTP entry for the admin dashboard's "suggest keyword changes" button.
    The Bearer middleware below already covers this path — but only when a
    token is configured; chat.keywords_advice adds its own fail-closed check
    for the token-unset case (see chat.answer's docstring for why). No
    request body — the advice is generated purely from current DB state."""
    from starlette.concurrency import run_in_threadpool

    import chat

    result, status = await run_in_threadpool(chat.keywords_advice)
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
