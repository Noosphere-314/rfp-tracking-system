"""Knowledge-base crawler — the "research mode" archive (KB-Module-Design.md).

This module is the kind='discourse' crawler specifically — worker/main.py's
KB_CRAWLERS registry (mirroring worker/fetchers/__init__.py's FETCHERS)
dispatches kb.forums rows here by `kind`, and kind='snapshot' rows to
worker/kb_snapshot.py instead. backfill(conn, client, forum, ...) and
incremental(conn, client, forum) share that same contract with the other
kind modules, so main.py's dispatch loop never has to special-case either.
repair() (below) is Discourse-only — no equivalent contract expected from the
other kind modules; worker/main.py checks `hasattr(crawler, "repair")`.

Three modes, all sharing the pipeline's per-host token bucket so a forum
never sees the RFP poller and the archiver as two separate aggressive clients:

  backfill     walk every (whitelisted) category page by page, fetch each
               topic's full post stream; resumable via kb.forums.backfill_cursor
  incremental  /posts.json (newest 50 posts site-wide) + /latest.json page 1
               for bumped topics; re-crawl whatever changed
  repair       one-off catch-up: re-crawl topics already in kb.topics whose
               stored kb.posts undershoot the topic's own post_count — see
               repair()'s docstring for the "partial capture, skipped
               forever" bug this recovers from

Only robots.txt-allowed JSON endpoints are used: /categories.json,
/c/<slug>/<id>.json, /t/<id>.json, /t/<id>/posts.json, /posts.json,
/latest.json, /about.json. Never /search or RSS (KB-Module-Design §3).

kb.topics.topic_id is `text` (migration 008): Snapshot proposal ids are
66-char hex hashes, not integers. Discourse ids are still plain ints at the
source (Discourse's own JSON), but every value that reaches kb.topics.topic_id
or a lookup keyed on it is normalised to `str` in this module — the DB always
returns `str` for that column now, and a stray `int` key would silently never
match a `str` key from the database, turning "topic already archived, skip
it" into "re-crawl everything, every run" without raising anything.
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

# kb.posts can legitimately fall a couple of rows short of kb.topics.post_count
# even on a fully-successful crawl: _store_posts() skips posts whose cooked+text
# are BOTH empty (e.g. a blanked/system post), by design. Without this slack a
# topic like that would look "incomplete" on every single completeness check
# forever, and repair() would re-fetch its tail on every run for no reason —
# so completeness checks (_known_topics, find_incomplete_topics) treat
# `have >= post_count - REPAIR_TOLERANCE` as "done", not `have == post_count`.
REPAIR_TOLERANCE = 2


@dataclass
class Forum:
    id: int
    forum_slug: str
    base_url: str
    kind: str
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
            # .get + fallback: defensive only — migration 008 backfills every
            # existing row with DEFAULT 'discourse', so a NULL should not occur.
            kind=row.get("kind") or "discourse",
            category_ids=row["category_ids"],
            backfill_done=row["backfill_done"],
            backfill_cursor=row["backfill_cursor"] or {},
            last_post_seen_at=row["last_post_seen_at"],
        )


@dataclass
class KnownTopic:
    """One kb.topics row's crawl state, as seen by backfill()/incremental()'s
    skip logic — see _known_topics()."""
    bumped_at: datetime | None
    complete: bool


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
            # topic_id колонка тепер text (міграція 008, під 66-символьні hex
            # id Snapshot) — стрінгуємо тут один раз, незалежно від того, чи
            # викликач уже передав str: bigint-id Discourse серіалізується
            # без втрат, і INSERT/ON CONFLICT завжди б'ють по тому самому типу.
            str(topic["id"]),
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


@dataclass
class CrawlResult:
    stored: int       # rows written to kb.posts this call (new + updated)
    complete: bool     # True iff every tail chunk fetched cleanly (see below)


def crawl_topic(
    conn: psycopg.Connection,
    client: HttpClient,
    forum: Forum,
    topic_id: int | str,
    category_name: str | None = None,
    listing_bumped_at: datetime | None = None,
) -> CrawlResult:
    """Fetch one topic completely (all posts) and upsert it.

    `listing_bumped_at` is the bumped_at the *listing* reported: /t/<id>.json
    itself has no bumped_at, and last_posted_at understates bumps that create
    no post (title edits, recategorisation). Storing the lower value would make
    the incremental pass re-crawl such topics every run, forever.

    `.complete` on the result is the OTHER half of that same problem: if the
    tail-chunk loop below hits a transient FetchError partway through a long
    topic, it stops (see the loop) — but by then _upsert_topic has already
    written the topic's real, correct bumped_at. A bumped_at-only "did this
    change?" check (which is all backfill()/incremental() used to have) would
    then see nothing wrong and skip the topic forever, freezing it at whatever
    partial post count the failure left it at. `.complete = False` lets callers
    know not to trust that partial result as "done" — see _known_topics() and
    repair().
    """
    response = client.get(f"{forum.base_url}/t/{topic_id}.json")
    if response.not_modified:
        return CrawlResult(stored=0, complete=True)
    data = response.json()
    # Discourse вміє відповісти 200 з літеральним `null` (прихована/щойно
    # видалена тема) — data стає None, і будь-який .get() валить ЦІЛИЙ прогін
    # форуму ('NoneType' object has no attribute 'get', Celo 2026-08-11,
    # прогін зупинився за 20 хв до кінця). Нема тіла — нема що зберігати;
    # complete=True, щоб примара не поверталась у кандидати вічно.
    if not isinstance(data, dict):
        log.warning("%s topic %s: empty/null topic JSON — skipped", forum.forum_slug, topic_id)
        return CrawlResult(stored=0, complete=True)

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
    complete = True
    for start in range(0, len(missing), POSTS_CHUNK):
        chunk = missing[start : start + POSTS_CHUNK]
        params = "&".join(f"post_ids[]={pid}" for pid in chunk)
        try:
            more = client.get(
                f"{forum.base_url}/t/{topic_id}/posts.json?{params}", use_cache=False
            )
        except FetchError as exc:
            log.warning("%s topic %s: posts chunk failed: %s", forum.forum_slug, topic_id, exc)
            # Не продовжуємо решту чанків: спроба їх забрати без цього одного
            # лише розмножує помилки. complete=False — сигнал нагору, що тема
            # лишилась частковою і НЕ повинна виглядати "готовою" для скіпу.
            complete = False
            break
        stored += _store_posts(
            conn, topic_ref, ((more.json() or {}).get("post_stream") or {}).get("posts") or []
        )

    conn.commit()
    return CrawlResult(stored=stored, complete=complete)


# ── Backfill ───────────────────────────────────────────────────────


def _categories(client: HttpClient, forum: Forum) -> list[dict]:
    """Flat category list (id, slug, name), honoring the whitelist if set.

    include_subcategories=true makes Discourse embed each top-level category's
    children under `subcategory_list`, and walk() recurses on whatever it
    finds there — so a 3rd-level category would be picked up too, PROVIDED
    Discourse actually nests that deep in the response. Verified live against
    ens.discuss.domains (2026-08-11): 9 top-level + 30 second-level categories,
    all reachable this way, `subcategory_list` empty at the leaves — no 3rd
    level was observed on any forum in kb.forums today. The warning below is a
    trip-wire in case some other forum ever does go 3 levels deep and
    Discourse doesn't embed that far: silently under-counting categories is
    exactly the ENS-29%-topics failure mode, and it would otherwise be
    invisible until someone compared against /about.json by hand again.
    """
    response = client.get(f"{forum.base_url}/categories.json?include_subcategories=true")
    payload = response.json() or {}
    flat: list[dict] = []

    def walk(categories: list[dict]) -> None:
        for category in categories:
            flat.append(
                {"id": category["id"], "slug": category["slug"], "name": category.get("name")}
            )
            children = category.get("subcategory_list") or []
            child_ids = category.get("subcategory_ids") or []
            if len(children) < len(child_ids):
                log.warning(
                    "%s: category %s advertises %d subcategory id(s) but only %d were "
                    "embedded — some topics may never be listed",
                    forum.forum_slug, category["id"], len(child_ids), len(children),
                )
            walk(children)

    walk((payload.get("category_list") or {}).get("categories") or [])

    if forum.category_ids:
        allowed = set(forum.category_ids)
        flat = [c for c in flat if c["id"] in allowed]
    return flat


def _known_topics(conn: psycopg.Connection, forum: Forum) -> dict[str, KnownTopic]:
    """topic_id (str) → KnownTopic, for the "did this change?" skip checks in
    backfill()/incremental().

    LEFT JOINs kb.posts to count actually-stored rows against kb.topics.
    post_count (written from the *remote* posts_count at crawl time — see
    _upsert_topic — so it does not drift just because a later crawl stalled).
    That count is what makes `.complete` trustworthy: a topic whose tail-chunk
    fetch failed (crawl_topic's `complete=False` case) still gets the correct
    bumped_at written immediately, so bumped_at alone can never tell "changed"
    from "was always broken" apart. Comparing the actual row count catches it.
    post_count IS NULL (e.g. a row from before this existed, or a source that
    never reports it) is treated as complete — nothing to compare against, and
    guessing "incomplete" would recrawl every such topic every run forever.
    """
    # topic_id — text у БД (міграція 008), тож psycopg повертає str-ключі тут.
    # Кожен виклик-майданчик нижче (backfill/incremental) звіряється з цим
    # словником і зобов'язаний так само стрінгувати свій бік порівняння.
    return {
        row["topic_id"]: KnownTopic(
            bumped_at=row["bumped_at"],
            complete=row["post_count"] is None
                     or row["have"] >= row["post_count"] - REPAIR_TOLERANCE,
        )
        for row in conn.execute(
            """
            SELECT t.topic_id, t.bumped_at, t.post_count, count(p.id) AS have
              FROM kb.topics t
              LEFT JOIN kb.posts p ON p.topic_ref = t.id
             WHERE t.forum_slug = %s
             GROUP BY t.id
            """,
            (forum.forum_slug,),
        )
    }


def find_incomplete_topics(conn: psycopg.Connection, forum: Forum) -> list[str]:
    """topic_id (str) list for repair(): topics whose stored kb.posts rows
    undershoot kb.topics.post_count by more than REPAIR_TOLERANCE.

    Same join as _known_topics(), just filtered down to the HAVING clause
    instead of returned in bulk — this is the backward-looking catch-up scan
    for topics that were already stuck incomplete BEFORE crawl_topic started
    reporting `.complete`, e.g. every forum backfilled prior to this fix.
    """
    rows = conn.execute(
        """
        SELECT t.topic_id
          FROM kb.topics t
          LEFT JOIN kb.posts p ON p.topic_ref = t.id
         WHERE t.forum_slug = %s AND t.post_count IS NOT NULL
         GROUP BY t.id
        HAVING count(p.id) < t.post_count - %s
         ORDER BY t.id
        """,
        (forum.forum_slug, REPAIR_TOLERANCE),
    ).fetchall()
    return [row["topic_id"] for row in rows]


def _update_remote_stats(conn: psycopg.Connection, client: HttpClient, forum: Forum) -> None:
    """Pull /about.json's topics_count/posts_count into kb.forums.remote_*.

    Found live 2026-08-11 (see DEVLOG): the archives are 14-55% complete on
    posts and nobody could see that without manually cross-checking against
    each forum's own /about.json. This puts the reference numbers right next
    to our own counts (kb.forums.remote_topics/remote_posts/stats_at, added by
    migration 012) so the gap is visible in `kb-status` / the admin panel
    going forward, on every backfill and incremental run.

    Best-effort and silent on failure: a blocked or slow /about.json is not a
    reason to abort the crawl that called this — the coverage numbers are a
    nice-to-have, the crawl itself is not.
    """
    try:
        response = client.get(f"{forum.base_url}/about.json", use_cache=False)
        about_stats = ((response.json() or {}).get("about") or {}).get("stats") or {}
    except (FetchError, SourceBlocked) as exc:
        log.warning("%s: /about.json stats fetch failed: %s", forum.forum_slug, exc)
        return
    conn.execute(
        "UPDATE kb.forums SET remote_topics = %s, remote_posts = %s, stats_at = now() "
        "WHERE id = %s",
        (about_stats.get("topics_count"), about_stats.get("posts_count"), forum.id),
    )
    conn.commit()


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
    _update_remote_stats(conn, client, forum)
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

            topics = (((response.json() or {}).get("topic_list") or {}).get("topics")) or []
            stats["pages_listed"] += 1
            if not topics:
                break

            interrupted = False
            for topic in topics:
                if should_stop() or (max_topics and stats["topics_crawled"] >= max_topics):
                    interrupted = True
                    break
                # str(): known (з _known_topics) — str-ключі, бо topic_id у БД
                # тепер text; порівнювати int-id зі свіжого лістингу проти
                # str-ключів завжди дало б "не знайдено" і перекроулило б
                # заново геть усе на кожному прогоні.
                topic_id = str(topic["id"])
                bumped = _ts(topic.get("bumped_at"))
                prior = known.get(topic_id)
                # `prior.complete` — root-cause fix for the "partial capture,
                # skipped forever" bug: a topic whose bumped_at hasn't moved is
                # normally settled and skippable, but if an earlier pass left
                # it incomplete (crawl_topic's tail-chunk fetch failed midway),
                # bumped_at alone can't tell that apart from "genuinely
                # unchanged" — see _known_topics(). Re-crawl it instead.
                if (
                    prior is not None and prior.complete
                    and prior.bumped_at and bumped and bumped <= prior.bumped_at
                ):
                    stats["skipped"] += 1
                    continue
                try:
                    result = crawl_topic(
                        conn, client, forum, topic_id, category.get("name"),
                        listing_bumped_at=bumped,
                    )
                    stats["posts_stored"] += result.stored
                    stats["topics_crawled"] += 1
                    known[topic_id] = KnownTopic(bumped_at=bumped, complete=result.complete)
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
    changed: dict[str, datetime | None] = {}   # str(topic_id) → listing bumped_at
    newest_seen = forum.last_post_seen_at

    _update_remote_stats(conn, client, forum)

    # New posts site-wide. One page spans days of activity on these forums, so
    # hourly polling cannot miss anything (KB-Module-Design §3).
    response = client.get(f"{forum.base_url}/posts.json", use_cache=False)
    for post in (response.json() or {}).get("latest_posts") or []:
        created = _ts(post.get("created_at"))
        if created and forum.last_post_seen_at and created <= forum.last_post_seen_at:
            continue
        if post.get("topic_id"):
            changed.setdefault(str(post["topic_id"]), None)
        if created and (newest_seen is None or created > newest_seen):
            newest_seen = created

    # Bumped topics (catches edits that create no new post but bump the topic).
    known = _known_topics(conn, forum)
    latest = client.get(f"{forum.base_url}/latest.json?order=activity", use_cache=False)
    for topic in (((latest.json() or {}).get("topic_list") or {}).get("topics")) or []:
        bumped = _ts(topic.get("bumped_at"))
        # str(): known — str-ключі (topic_id text у БД, див. _known_topics).
        prior = known.get(str(topic["id"]))
        stored_bumped = prior.bumped_at if prior else None
        # Same root-cause fix as backfill(): a topic /latest.json shows as
        # unchanged can still be one crawl_topic() left incomplete earlier
        # (chunk fetch failed). Re-crawl it here too instead of waiting for a
        # separate repair() run — incremental already paid for the listing.
        stuck_incomplete = prior is not None and not prior.complete
        if stuck_incomplete or (bumped and (stored_bumped is None or bumped > stored_bumped)):
            changed[str(topic["id"])] = bumped

    for topic_id, listing_bumped in changed.items():
        try:
            result = crawl_topic(
                conn, client, forum, topic_id, listing_bumped_at=listing_bumped
            )
            stats["posts_stored"] += result.stored
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


# ── Repair ─────────────────────────────────────────────────────────


def repair(
    conn: psycopg.Connection,
    client: HttpClient,
    forum: Forum,
    max_topics: int | None = None,
    should_stop=lambda: False,
) -> dict[str, int]:
    """Catch-up pass: re-crawl topics already in kb.topics whose stored
    kb.posts rows undershoot kb.topics.post_count (find_incomplete_topics()).

    This is the backward-looking half of the "partial capture, skipped
    forever" fix (see crawl_topic()'s and _known_topics()'s docstrings for the
    forward-looking half). Every topic archived before this fix landed has no
    recorded `.complete` — for those, this is the only way back to a full
    archive short of a full re-backfill. Safe to run repeatedly and safe to
    run on a forum whose ordinary backfill is still in progress: crawl_topic()
    is a straight upsert (ON CONFLICT DO UPDATE), so re-fetching an
    already-complete topic just re-writes the same rows.

    Unlike backfill(), this does not walk category listings or touch
    backfill_cursor/backfill_done — it works purely off what is already in
    kb.topics, so it has nothing to resume: an interrupted run just leaves the
    remaining candidates for the next run to pick up (find_incomplete_topics()
    re-derives the candidate list fresh every call).
    """
    stats = {"candidates": 0, "topics_crawled": 0, "posts_stored": 0,
              "still_incomplete": 0, "failed": 0}
    candidates = find_incomplete_topics(conn, forum)
    stats["candidates"] = len(candidates)
    log.info(
        "%s repair: %d topic(s) below post_count (tolerance %d)",
        forum.forum_slug, len(candidates), REPAIR_TOLERANCE,
    )

    for topic_id in candidates:
        if should_stop() or (max_topics and stats["topics_crawled"] >= max_topics):
            break
        try:
            result = crawl_topic(conn, client, forum, topic_id)
            stats["posts_stored"] += result.stored
            stats["topics_crawled"] += 1
            if not result.complete:
                stats["still_incomplete"] += 1
        except SourceBlocked:
            raise  # 403/429 must stop the whole forum, same as backfill()
        except FetchError as exc:
            stats["failed"] += 1
            log.warning("%s repair topic %s: %s", forum.forum_slug, topic_id, exc)

    log.info("%s repair pass: %s", forum.forum_slug, stats)
    return stats


def record_failure(conn: psycopg.Connection, forum: Forum, error: Exception) -> None:
    conn.rollback()
    conn.execute(
        "UPDATE kb.forums SET consecutive_failures = consecutive_failures + 1 "
        "WHERE id = %s",
        (forum.id,),
    )
    conn.commit()
    # exception, не error: record_failure завжди кличуть ЗСЕРЕДИНИ except,
    # тож активний контекст винятку дає повний трейсбек. Без нього Celo
    # 2026-08-12 дебажили наосліп: «'NoneType' object has no attribute
    # 'get'» без жодного натяку, ДЕ саме.
    log.exception("%s: crawl failed: %s", forum.forum_slug, error)


def status(conn: psycopg.Connection) -> list[dict]:
    # remote_topics/remote_posts/stats_at (migration 012) — the /about.json
    # reference numbers _update_remote_stats() writes on every backfill/
    # incremental run, so the coverage gap (found 2026-08-11: posts archives
    # 14-55% complete) shows up here instead of needing a manual cross-check.
    return conn.execute(
        """
        SELECT f.forum_slug, f.enabled, f.backfill_done, f.last_incremental_at,
               f.consecutive_failures, f.remote_topics, f.remote_posts, f.stats_at,
               count(DISTINCT t.id) AS topics,
               count(p.id) AS posts,
               max(t.bumped_at) AS newest_activity
          FROM kb.forums f
          LEFT JOIN kb.topics t ON t.forum_slug = f.forum_slug
          LEFT JOIN kb.posts p ON p.topic_ref = t.id
         GROUP BY f.id ORDER BY f.id
        """
    ).fetchall()
