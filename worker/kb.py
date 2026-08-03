"""Knowledge-base crawler — the "research mode" archive (KB-Module-Design.md).

Two modes, both sharing the pipeline's per-host token bucket so a forum never
sees the RFP poller and the archiver as two separate aggressive clients:

  backfill     walk every (whitelisted) category page by page, fetch each
               topic's full post stream; resumable via kb.forums.backfill_cursor
  incremental  /posts.json (newest 50 posts site-wide) + /latest.json page 1
               for bumped topics; re-crawl whatever changed

Only robots.txt-allowed JSON endpoints are used: /categories.json,
/c/<slug>/<id>.json, /t/<id>.json, /t/<id>/posts.json, /posts.json,
/latest.json. Never /search or RSS (KB-Module-Design §3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import psycopg

from .http import FetchError, HttpClient, SourceBlocked
from .items import strip_html

log = logging.getLogger(__name__)

POSTS_CHUNK = 50          # /t/<id>/posts.json accepts a post_ids[] batch
LISTING_SAFETY_PAGES = 400  # hard stop per category — no forum has 12k pages


@dataclass
class Forum:
    id: int
    forum_slug: str
    base_url: str
    category_ids: list[int] | None
    backfill_done: bool
    backfill_cursor: dict[str, Any]
    last_post_seen_at: datetime | None

    @classmethod
    def from_row(cls, row: dict) -> "Forum":
        return cls(
            id=row["id"],
            forum_slug=row["forum_slug"],
            base_url=row["base_url"].rstrip("/"),
            category_ids=row["category_ids"],
            backfill_done=row["backfill_done"],
            backfill_cursor=row["backfill_cursor"] or {},
            last_post_seen_at=row["last_post_seen_at"],
        )


def _ts(value: str | datetime | None) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_forums(conn: psycopg.Connection, only_slug: str | None = None) -> list[Forum]:
    rows = conn.execute(
        "SELECT * FROM kb.forums WHERE enabled AND (%s::text IS NULL OR forum_slug = %s) "
        "ORDER BY id",
        (only_slug, only_slug),
    ).fetchall()
    return [Forum.from_row(row) for row in rows]


# ── Topic ingestion ────────────────────────────────────────────────


def _upsert_topic(
    conn: psycopg.Connection, forum: Forum, topic: dict, category_name: str | None
) -> int:
    row = conn.execute(
        """
        INSERT INTO kb.topics (forum_slug, topic_id, category_id, category_name,
                               title, url, author, created_at, bumped_at,
                               post_count, last_crawled_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (forum_slug, topic_id) DO UPDATE
            SET title = EXCLUDED.title,
                category_id = COALESCE(EXCLUDED.category_id, kb.topics.category_id),
                category_name = COALESCE(EXCLUDED.category_name, kb.topics.category_name),
                bumped_at = EXCLUDED.bumped_at,
                post_count = EXCLUDED.post_count,
                last_crawled_at = now()
        RETURNING id
        """,
        (
            forum.forum_slug,
            topic["id"],
            topic.get("category_id"),
            category_name,
            topic.get("title") or topic.get("fancy_title") or f"topic {topic['id']}",
            f"{forum.base_url}/t/{topic.get('slug', 'topic')}/{topic['id']}",
            topic.get("author"),
            _ts(topic.get("created_at")),
            _ts(topic.get("bumped_at") or topic.get("last_posted_at")),
            topic.get("posts_count"),
        ),
    ).fetchone()
    return row["id"]


def _store_posts(conn: psycopg.Connection, topic_ref: int, posts: Iterable[dict]) -> int:
    stored = 0
    for post in posts:
        cooked = post.get("cooked") or ""
        text = strip_html(cooked)
        if not text and not cooked:
            continue
        conn.execute(
            """
            INSERT INTO kb.posts (topic_ref, post_number, author, posted_at,
                                  edited_at, raw_text, raw_html)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (topic_ref, post_number) DO UPDATE
                SET raw_text = EXCLUDED.raw_text,
                    raw_html = EXCLUDED.raw_html,
                    edited_at = EXCLUDED.edited_at,
                    author = EXCLUDED.author
            """,
            (
                topic_ref,
                post.get("post_number", 0),
                post.get("username") or post.get("name"),
                _ts(post.get("created_at")),
                _ts(post.get("updated_at")),
                text,
                cooked,
            ),
        )
        stored += 1
    return stored


def crawl_topic(
    conn: psycopg.Connection,
    client: HttpClient,
    forum: Forum,
    topic_id: int,
    category_name: str | None = None,
    listing_bumped_at: datetime | None = None,
) -> int:
    """Fetch one topic completely (all posts) and upsert it. Returns post count.

    `listing_bumped_at` is the bumped_at the *listing* reported: /t/<id>.json
    itself has no bumped_at, and last_posted_at understates bumps that create
    no post (title edits, recategorisation). Storing the lower value would make
    the incremental pass re-crawl such topics every run, forever.
    """
    response = client.get(f"{forum.base_url}/t/{topic_id}.json")
    if response.not_modified:
        return 0
    data = response.json()

    stream: list[int] = (data.get("post_stream") or {}).get("stream") or []
    included: list[dict] = (data.get("post_stream") or {}).get("posts") or []

    topic_ref = _upsert_topic(
        conn,
        forum,
        {
            "id": data.get("id", topic_id),
            "title": data.get("title"),
            "slug": data.get("slug"),
            "category_id": data.get("category_id"),
            "created_at": data.get("created_at"),
            "bumped_at": listing_bumped_at
                         or data.get("last_posted_at")
                         or data.get("created_at"),
            "posts_count": data.get("posts_count"),
            "author": (data.get("details") or {}).get("created_by", {}).get("username"),
        },
        category_name,
    )

    stored = _store_posts(conn, topic_ref, included)

    # Long topics: /t/<id>.json embeds only the first ~20 posts; the rest come
    # in chunks by explicit id list.
    have = {p.get("id") for p in included}
    missing = [pid for pid in stream if pid not in have]
    for start in range(0, len(missing), POSTS_CHUNK):
        chunk = missing[start : start + POSTS_CHUNK]
        params = "&".join(f"post_ids[]={pid}" for pid in chunk)
        try:
            more = client.get(
                f"{forum.base_url}/t/{topic_id}/posts.json?{params}", use_cache=False
            )
        except FetchError as exc:
            log.warning("%s topic %s: posts chunk failed: %s", forum.forum_slug, topic_id, exc)
            break
        stored += _store_posts(
            conn, topic_ref, (more.json().get("post_stream") or {}).get("posts") or []
        )

    conn.commit()
    return stored


# ── Backfill ───────────────────────────────────────────────────────


def _categories(client: HttpClient, forum: Forum) -> list[dict]:
    """Flat category list (id, slug, name), honoring the whitelist if set."""
    response = client.get(f"{forum.base_url}/categories.json?include_subcategories=true")
    payload = response.json()
    flat: list[dict] = []

    def walk(categories: list[dict]) -> None:
        for category in categories:
            flat.append(
                {"id": category["id"], "slug": category["slug"], "name": category.get("name")}
            )
            walk(category.get("subcategory_list") or [])

    walk((payload.get("category_list") or {}).get("categories") or [])

    if forum.category_ids:
        allowed = set(forum.category_ids)
        flat = [c for c in flat if c["id"] in allowed]
    return flat


def _known_topics(conn: psycopg.Connection, forum: Forum) -> dict[int, datetime | None]:
    return {
        row["topic_id"]: row["bumped_at"]
        for row in conn.execute(
            "SELECT topic_id, bumped_at FROM kb.topics WHERE forum_slug = %s",
            (forum.forum_slug,),
        )
    }


def _save_cursor(conn: psycopg.Connection, forum: Forum, cursor: dict) -> None:
    import json

    conn.execute(
        "UPDATE kb.forums SET backfill_cursor = %s WHERE id = %s",
        (json.dumps(cursor), forum.id),
    )
    conn.commit()


def backfill(
    conn: psycopg.Connection,
    client: HttpClient,
    forum: Forum,
    max_topics: int | None = None,
    should_stop=lambda: False,
) -> dict[str, int]:
    """Walk categories page by page; crawl new/changed topics. Resumable.

    Commits after every topic and saves the cursor after every listing page, so
    a SIGTERM or crash costs at most one page of re-listing, never re-crawling.
    """
    stats = {"topics_crawled": 0, "posts_stored": 0, "pages_listed": 0, "skipped": 0}
    categories = _categories(client, forum)
    known = _known_topics(conn, forum)
    done_ids: set[int] = set(forum.backfill_cursor.get("done_category_ids", []))
    resume_page = int(forum.backfill_cursor.get("page", 0))
    resume_category = forum.backfill_cursor.get("current_category_id")

    log.info(
        "%s backfill: %d categor(ies), %d already archived topic(s)",
        forum.forum_slug, len(categories), len(known),
    )

    def cursor_at(category_id: int, page: int) -> dict:
        return {
            "done_category_ids": sorted(done_ids),
            "current_category_id": category_id,
            "page": page,
        }

    for category in categories:
        if category["id"] in done_ids:
            continue
        page = resume_page if category["id"] == resume_category else 0
        resume_page = 0

        while page < LISTING_SAFETY_PAGES:
            if should_stop() or (max_topics and stats["topics_crawled"] >= max_topics):
                _save_cursor(conn, forum, cursor_at(category["id"], page))
                return stats

            url = f"{forum.base_url}/c/{category['slug']}/{category['id']}.json?page={page}"
            try:
                response = client.get(url, use_cache=False)
            except (FetchError, SourceBlocked):
                # A failed listing must never let the category be marked done:
                # the unread pages would be silently lost forever. Save the
                # exact position and stop the forum; a re-run resumes here.
                _save_cursor(conn, forum, cursor_at(category["id"], page))
                raise

            topics = ((response.json().get("topic_list") or {}).get("topics")) or []
            stats["pages_listed"] += 1
            if not topics:
                break

            interrupted = False
            for topic in topics:
                if should_stop() or (max_topics and stats["topics_crawled"] >= max_topics):
                    interrupted = True
                    break
                topic_id = topic["id"]
                bumped = _ts(topic.get("bumped_at"))
                if topic_id in known and known[topic_id] and bumped and bumped <= known[topic_id]:
                    stats["skipped"] += 1
                    continue
                try:
                    stats["posts_stored"] += crawl_topic(
                        conn, client, forum, topic_id, category.get("name"),
                        listing_bumped_at=bumped,
                    )
                    stats["topics_crawled"] += 1
                    known[topic_id] = bumped
                except SourceBlocked:
                    _save_cursor(conn, forum, cursor_at(category["id"], page))
                    raise  # 403/429 must stop the whole forum, not one topic
                except FetchError as exc:
                    log.warning("%s topic %s: %s", forum.forum_slug, topic_id, exc)

                if stats["topics_crawled"] and stats["topics_crawled"] % 100 == 0:
                    log.info(
                        "%s backfill progress: %d topics, %d posts",
                        forum.forum_slug, stats["topics_crawled"], stats["posts_stored"],
                    )

            if interrupted:
                # Stopped mid-page: the cursor must point at THIS page, not the
                # next one, or the untouched tail of the page is lost. Resume
                # re-lists it and the `known` check skips what is already in.
                _save_cursor(conn, forum, cursor_at(category["id"], page))
                return stats

            page += 1
            _save_cursor(conn, forum, cursor_at(category["id"], page))

        done_ids.add(category["id"])
        _save_cursor(conn, forum, {"done_category_ids": sorted(done_ids)})

    conn.execute(
        "UPDATE kb.forums SET backfill_done = true, backfill_cursor = '{}', "
        "consecutive_failures = 0 WHERE id = %s",
        (forum.id,),
    )
    conn.commit()
    log.info("%s backfill COMPLETE: %s", forum.forum_slug, stats)
    return stats


# ── Incremental ────────────────────────────────────────────────────


def incremental(
    conn: psycopg.Connection, client: HttpClient, forum: Forum
) -> dict[str, int]:
    """Hourly-friendly update: new posts + bumped topics since the watermark."""
    stats = {"topics_crawled": 0, "posts_stored": 0, "failed": 0}
    changed: dict[int, datetime | None] = {}   # topic_id → listing bumped_at
    newest_seen = forum.last_post_seen_at

    # New posts site-wide. One page spans days of activity on these forums, so
    # hourly polling cannot miss anything (KB-Module-Design §3).
    response = client.get(f"{forum.base_url}/posts.json", use_cache=False)
    for post in response.json().get("latest_posts") or []:
        created = _ts(post.get("created_at"))
        if created and forum.last_post_seen_at and created <= forum.last_post_seen_at:
            continue
        if post.get("topic_id"):
            changed.setdefault(post["topic_id"], None)
        if created and (newest_seen is None or created > newest_seen):
            newest_seen = created

    # Bumped topics (catches edits that create no new post but bump the topic).
    known = _known_topics(conn, forum)
    latest = client.get(f"{forum.base_url}/latest.json?order=activity", use_cache=False)
    for topic in ((latest.json().get("topic_list") or {}).get("topics")) or []:
        bumped = _ts(topic.get("bumped_at"))
        stored = known.get(topic["id"])
        if bumped and (stored is None or bumped > stored):
            changed[topic["id"]] = bumped

    for topic_id, listing_bumped in changed.items():
        try:
            stats["posts_stored"] += crawl_topic(
                conn, client, forum, topic_id, listing_bumped_at=listing_bumped
            )
            stats["topics_crawled"] += 1
        except FetchError as exc:
            stats["failed"] += 1
            log.warning("%s incremental topic %s: %s", forum.forum_slug, topic_id, exc)

    # Advance the /posts.json watermark only on a clean pass: a failed topic
    # whose posts sit behind an advanced watermark would need to reappear on
    # /latest page 1 to ever be recovered.
    conn.execute(
        "UPDATE kb.forums SET last_post_seen_at = COALESCE(%s, last_post_seen_at), "
        "last_incremental_at = now(), consecutive_failures = 0 WHERE id = %s",
        (newest_seen if stats["failed"] == 0 else forum.last_post_seen_at, forum.id),
    )
    conn.commit()
    return stats


def record_failure(conn: psycopg.Connection, forum: Forum, error: Exception) -> None:
    conn.rollback()
    conn.execute(
        "UPDATE kb.forums SET consecutive_failures = consecutive_failures + 1 "
        "WHERE id = %s",
        (forum.id,),
    )
    conn.commit()
    log.error("%s: crawl failed: %s", forum.forum_slug, error)


def status(conn: psycopg.Connection) -> list[dict]:
    return conn.execute(
        """
        SELECT f.forum_slug, f.enabled, f.backfill_done, f.last_incremental_at,
               f.consecutive_failures,
               count(DISTINCT t.id) AS topics,
               count(p.id) AS posts,
               max(t.bumped_at) AS newest_activity
          FROM kb.forums f
          LEFT JOIN kb.topics t ON t.forum_slug = f.forum_slug
          LEFT JOIN kb.posts p ON p.topic_ref = t.id
         GROUP BY f.id ORDER BY f.id
        """
    ).fetchall()
