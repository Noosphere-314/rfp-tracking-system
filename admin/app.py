"""Engineer-facing admin dashboard.

Access model mirrors the n8n editor (System-Design.md §9): this service is
never exposed through Caddy — it binds to localhost on the box and is reached
over Tailscale or an SSH tunnel. Non-engineers keep using the n8n forms; this
panel is for the people running the system: health, source management with a
live test-fetch, keyword/threshold editing, and item/run inspection.

It also hosts the local-dev mock of the n8n pipeline (MOCK_N8N=true): the
worker can point N8N_WEBHOOK_URL here and the full deliver→confirm loop runs
end-to-end with no Pipedrive, no Claude and no n8n.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import regex
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row

from worker import fetchers
from worker.fetchers.base import Source
from worker.http import HttpClient, SourceBlocked

log = logging.getLogger("admin")

DATABASE_URL = os.environ["DATABASE_URL"]
MOCK_N8N = os.environ.get("MOCK_N8N", "").lower() in ("1", "true", "yes")
WEBHOOK_SECRET = os.environ.get("N8N_WEBHOOK_SECRET", "")

app = FastAPI(title="RFP Tracker Admin", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def db() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, client_encoding="utf8")


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
            "SELECT * FROM worker_runs ORDER BY started_at DESC LIMIT 10"
        ).fetchall()
        source_health = conn.execute(
            """
            SELECT s.id, s.name, s.type, s.ecosystem, s.enabled, s.quarantined,
                   s.lane, s.last_success_at, s.last_item_at, s.consecutive_failures,
                   count(i.item_uid) FILTER (WHERE i.first_seen > now() - interval '7 days') AS items_7d
              FROM sources s LEFT JOIN seen_items i ON i.source_id = s.id
             GROUP BY s.id ORDER BY s.enabled DESC, s.name
            """
        ).fetchall()
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
            "status_counts": status_counts,
            "last_runs": last_runs,
            "source_health": source_health,
            "recent_pending": recent_pending,
            "mock_n8n": MOCK_N8N,
        },
    )


# ── Sources ────────────────────────────────────────────────────────


@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, message: str = "", error: str = ""):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM sources ORDER BY enabled DESC, quarantined, type, name"
        ).fetchall()
    return templates.TemplateResponse(
        request,
        "sources.html",
        {
            "sources": rows,
            "fetcher_types": sorted(fetchers.FETCHERS),
            "message": message,
            "error": error,
        },
    )


@app.post("/sources/{source_id}/toggle")
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


@app.post("/sources/add")
def add_source(
    type: str = Form(...),
    name: str = Form(...),
    ecosystem: str = Form(...),
    url: str = Form(...),
    category: str = Form(""),
    lane: str = Form("rfp"),
    config: str = Form("{}"),
):
    """Add with live test-fetch — the same guarantee the n8n form gives:
    a source that cannot produce items right now is not saved as enabled."""
    if type not in fetchers.FETCHERS:
        return RedirectResponse(f"/sources?error=unknown+type+{type}", status_code=303)
    try:
        config_obj = json.loads(config or "{}")
        if not isinstance(config_obj, dict):
            raise ValueError("config must be a JSON object")
    except ValueError as exc:
        return RedirectResponse(f"/sources?error=bad+config:+{exc}", status_code=303)

    candidate = {
        "id": 0, "type": type, "name": name.strip(), "ecosystem": ecosystem.strip(),
        "url": url.strip(), "category": category.strip() or None,
        "config": config_obj, "lane": lane,
    }
    count, error = _test_fetch(candidate)
    if error:
        return RedirectResponse(f"/sources?error=test-fetch failed: {error}", status_code=303)

    with db() as conn:
        conn.execute(
            """
            INSERT INTO sources (type, name, ecosystem, url, category, config, lane,
                                 enabled, added_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, true, 'admin-ui')
            ON CONFLICT (type, url, category) DO UPDATE
                SET name = EXCLUDED.name, config = EXCLUDED.config,
                    enabled = true, quarantined = false, quarantine_reason = NULL
            """,
            (type, candidate["name"], candidate["ecosystem"], candidate["url"],
             candidate["category"], json.dumps(config_obj), lane),
        )
        conn.commit()
    return RedirectResponse(
        f"/sources?message=saved — test-fetch returned {count} item(s)", status_code=303
    )


# ── Keywords ───────────────────────────────────────────────────────


@app.get("/keywords", response_class=HTMLResponse)
def keywords_page(request: Request, message: str = "", error: str = ""):
    with db() as conn:
        rows = conn.execute("SELECT * FROM keywords ORDER BY kind, id").fetchall()
    return templates.TemplateResponse(
        request, "keywords.html", {"keywords": rows, "message": message, "error": error}
    )


@app.post("/keywords/add")
def add_keyword(pattern: str = Form(...), kind: str = Form(...)):
    if kind not in ("include", "exclude"):
        raise HTTPException(400, "kind must be include or exclude")
    # Compile-check at write time (A4); read-time quarantine still applies.
    try:
        regex.compile(pattern, regex.IGNORECASE)
    except regex.error as exc:
        return RedirectResponse(f"/keywords?error=bad pattern: {exc}", status_code=303)

    with db() as conn:
        conn.execute(
            "INSERT INTO keywords (pattern, kind, added_by) VALUES (%s, %s, 'admin-ui') "
            "ON CONFLICT (pattern, kind) DO UPDATE SET enabled = true",
            (pattern, kind),
        )
        conn.commit()
    return RedirectResponse("/keywords?message=saved", status_code=303)


@app.post("/keywords/{keyword_id}/toggle")
def toggle_keyword(keyword_id: int):
    with db() as conn:
        conn.execute(
            "UPDATE keywords SET enabled = NOT enabled WHERE id = %s", (keyword_id,)
        )
        conn.commit()
    return RedirectResponse("/keywords", status_code=303)


# ── Settings ───────────────────────────────────────────────────────


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, message: str = ""):
    with db() as conn:
        rows = conn.execute("SELECT * FROM settings ORDER BY key").fetchall()
    return templates.TemplateResponse(
        request, "settings.html", {"settings": rows, "message": message}
    )


@app.post("/settings/save")
async def save_settings(request: Request):
    form = await request.form()
    with db() as conn:
        for key, value in form.items():
            conn.execute(
                "UPDATE settings SET value = %s, updated_at = now(), "
                "updated_by = 'admin-ui' WHERE key = %s AND value IS DISTINCT FROM %s",
                (value, key, value),
            )
        conn.commit()
    return RedirectResponse("/settings?message=saved", status_code=303)


# ── Items & runs ───────────────────────────────────────────────────


@app.get("/items", response_class=HTMLResponse)
def items_page(request: Request, status: str = "", source_id: int = 0, page: int = 0):
    where, params = [], []
    if status:
        where.append("i.status = %s")
        params.append(status)
    if source_id:
        where.append("i.source_id = %s")
        params.append(source_id)
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
            "items": rows, "status": status, "source_id": source_id,
            "page": page, "source_options": source_options,
        },
    )


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM worker_runs ORDER BY started_at DESC LIMIT 50"
        ).fetchall()
    return templates.TemplateResponse(request, "runs.html", {"runs": rows})


# ── Knowledge base ─────────────────────────────────────────────────


@app.get("/kb", response_class=HTMLResponse)
def kb_page(request: Request, q: str = "", forum: str = ""):
    with db() as conn:
        forums = conn.execute(
            """
            SELECT f.*, count(DISTINCT t.id) AS topics, count(p.id) AS posts,
                   max(t.bumped_at) AS newest_activity
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
                       -- «»-markers, not <b>: raw_text can contain literal
                       -- '<script>' (forum code blocks, entities decoded at
                       -- ingest), so the template must autoescape the snippet.
                       ts_headline('english', p.raw_text,
                                   websearch_to_tsquery('english', %(q)s),
                                   'MaxWords=35, MinWords=15, StartSel=«, StopSel=»') AS snippet
                  FROM kb.posts p JOIN kb.topics t ON t.id = p.topic_ref
                 WHERE p.body_tsv @@ websearch_to_tsquery('english', %(q)s)
                   AND (%(forum)s = '' OR t.forum_slug = %(forum)s)
                 ORDER BY ts_rank_cd(p.body_tsv,
                                     websearch_to_tsquery('english', %(q)s)) DESC
                 LIMIT 25
                """,
                {"q": q, "forum": forum},
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


@app.post("/kb/forums/{forum_id}/toggle")
def toggle_kb_forum(forum_id: int):
    with db() as conn:
        conn.execute(
            "UPDATE kb.forums SET enabled = NOT enabled WHERE id = %s", (forum_id,)
        )
        conn.commit()
    return RedirectResponse("/kb", status_code=303)


@app.post("/kb/forums/add")
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


@app.post("/items/{item_uid}/brief")
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


@app.get("/briefs/{brief_id}", response_class=HTMLResponse)
def view_brief(request: Request, brief_id: int):
    with db() as conn:
        brief = conn.execute(
            "SELECT * FROM kb.briefs WHERE id = %s", (brief_id,)
        ).fetchone()
    if not brief:
        raise HTTPException(404, "brief not found")
    return templates.TemplateResponse(request, "brief.html", {"brief": brief})


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
