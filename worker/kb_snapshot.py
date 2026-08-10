"""Knowledge-base crawler for kb.forums.kind = 'snapshot' (KB-Module-Design.md
extension, migration 008).

Same backfill(conn, client, forum, ...) / incremental(conn, client, forum)
contract as worker/kb.py's Discourse crawler, on purpose: worker/main.py's
KB_CRAWLERS registry (mirroring worker/fetchers/__init__.py's FETCHERS)
dispatches on kb.forums.kind without special-casing either module. `client`
is the same shared, SSRF-checked, DB-backed-rate-limited HttpClient the
pipeline fetchers use (worker/http.py, worker/ratelimit.py) — hub.snapshot.org
gets ONE shared token bucket whether the hourly pipeline poll or this
overnight KB backfill is the one hitting it (host_rate_limits, keyed by host).

One kb.forums row = one Snapshot space. Unlike Discourse (identified by
base_url), the space id lives in backfill_cursor->>'space' (set once by the
migration 008 seed and never cleared — only the resumable 'skip' sub-key is
reset when a backfill finishes). Query shape mirrors
worker/fetchers/snapshot.py (the pipeline's own Snapshot fetcher): same
GraphQL endpoint, same x-api-key header when SNAPSHOT_API_KEY is set.

НЕ зберігаємо окремі голоси НІКОЛИ (5.68M рядків голосів у самому лише
arbitrumfoundation.eth — перевірено наживо 2026-08-07). `votes`/`scores`/
`scores_state` — уже готові агрегати в самому proposal-об'єкті GraphQL-відповіді
— йдуть у текстовий футер одного kb.posts-рядка на пропозицію, і більше нічого
голосового звідси не читається.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import psycopg

from .config import config
from .http import FetchError, HttpClient
from .kb import Forum

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://hub.snapshot.org/graphql"

PAGE_SIZE = 100                  # backfill page — Snapshot's own max page size
INCREMENTAL_PAGE_SIZE = 20       # "small pages": hourly catch-up is a handful
LISTING_SAFETY_PAGES = 500       # runaway-loop breaker, mirrors kb.py's per-forum cap
INCREMENTAL_SAFETY_PAGES = 50    # ditto, scaled down for the hourly path

# Без createdGt/skip-фільтра — повний прохід простору, від початку або з
# точки резюме (backfill_cursor->>'skip').
QUERY_PAGE = """
query Proposals($space: [String!], $first: Int!, $skip: Int!) {
  proposals(
    first: $first
    skip: $skip
    where: { space_in: $space }
    orderBy: "created"
    orderDirection: asc
  ) {
    id title body author created end state link discussion
    choices scores scores_total scores_state votes
    space { id }
  }
}
"""

# Той самий shape, плюс created_gt — інкрементальний прохід від водяного знаку.
QUERY_SINCE = """
query ProposalsSince($space: [String!], $createdGt: Int!, $first: Int!, $skip: Int!) {
  proposals(
    first: $first
    skip: $skip
    where: { space_in: $space, created_gt: $createdGt }
    orderBy: "created"
    orderDirection: asc
  ) {
    id title body author created end state link discussion
    choices scores scores_total scores_state votes
    space { id }
  }
}
"""


def _headers() -> dict[str, str]:
    headers = {}
    if config.snapshot_api_key:
        headers["x-api-key"] = config.snapshot_api_key
    return headers


def _epoch_to_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


def _footer(proposal: dict[str, Any]) -> str:
    """Плейн-текстовий підсумок голосування, що йде в кінець raw_text.

    Формат за специфікацією: "— Votes: N · Result: <choice: score, ...>
    (<scores_state>)" плюс необов'язковий рядок "Discussion: <url>". Це
    ЄДИНЕ місце, куди потрапляють голоси — вже агреговані Snapshot'ом,
    жодного окремого vote-рядка ніде не зберігається.
    """
    choices = proposal.get("choices") or []
    scores = proposal.get("scores") or []
    pairs = [f"{choice}: {score:.2f}" for choice, score in zip(choices, scores)]
    result = ", ".join(pairs) if pairs else "n/a"
    votes = proposal.get("votes") or 0
    state = proposal.get("scores_state") or "unknown"
    lines = [f"— Votes: {votes} · Result: {result} ({state})"]
    discussion = proposal.get("discussion")
    if discussion:
        lines.append(f"Discussion: {discussion}")
    return "\n".join(lines)


def _proposal_url(proposal: dict[str, Any], space_id: str) -> str:
    return (
        proposal.get("link")
        or f"https://snapshot.org/#/{space_id}/proposal/{proposal['id']}"
    )


def _upsert_proposal(conn: psycopg.Connection, forum: Forum, proposal: dict[str, Any]) -> None:
    """One proposal → one kb.topics row + one kb.posts row (post_number=1).

    ON CONFLICT (forum_slug, topic_id) DO UPDATE: an OPEN proposal's state and
    scores change on every re-crawl until it closes, so title/category_name/
    bumped_at and the post's footer must refresh, not just insert-once.
    topic_id is stored via str(): kb.topics.topic_id is `text` (migration
    008) precisely because these ids are 66-char hex hashes, not integers.
    """
    space_id = (proposal.get("space") or {}).get("id") or forum.backfill_cursor.get("space", "")
    topic_id = str(proposal["id"])
    body = (proposal.get("body") or "").strip()
    footer = _footer(proposal)
    raw_text = f"{body}\n\n{footer}" if body else footer
    created_at = _epoch_to_dt(proposal.get("created"))

    topic_row = conn.execute(
        """
        INSERT INTO kb.topics (forum_slug, topic_id, category_id, category_name,
                               title, url, author, created_at, bumped_at,
                               post_count, last_crawled_at)
        VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s, 1, now())
        ON CONFLICT (forum_slug, topic_id) DO UPDATE
            SET title = EXCLUDED.title,
                category_name = EXCLUDED.category_name,
                bumped_at = EXCLUDED.bumped_at,
                last_crawled_at = now()
        RETURNING id
        """,
        (
            forum.forum_slug,
            topic_id,
            proposal.get("state"),
            proposal.get("title") or f"proposal {topic_id}",
            _proposal_url(proposal, space_id),
            proposal.get("author"),
            created_at,
            _epoch_to_dt(proposal.get("end")),
        ),
    ).fetchone()
    topic_ref = topic_row["id"]

    conn.execute(
        """
        INSERT INTO kb.posts (topic_ref, post_number, author, posted_at,
                              edited_at, raw_text, raw_html)
        VALUES (%s, 1, %s, %s, NULL, %s, NULL)
        ON CONFLICT (topic_ref, post_number) DO UPDATE
            SET raw_text = EXCLUDED.raw_text,
                edited_at = EXCLUDED.edited_at
        """,
        (topic_ref, proposal.get("author"), created_at, raw_text),
    )


def _save_cursor(conn: psycopg.Connection, forum: Forum, cursor: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE kb.forums SET backfill_cursor = %s WHERE id = %s",
        (json.dumps(cursor), forum.id),
    )
    conn.commit()


def _run_query(client: HttpClient, query: str, variables: dict[str, Any]) -> list[dict]:
    response = client.post_json(
        GRAPHQL_URL, {"query": query, "variables": variables}, headers=_headers()
    )
    payload = response.json()
    if payload.get("errors"):
        raise FetchError(f"snapshot GraphQL errors: {payload['errors']}")
    return (payload.get("data") or {}).get("proposals") or []


def backfill(
    conn: psycopg.Connection,
    client: HttpClient,
    forum: Forum,
    max_topics: int | None = None,
    should_stop=lambda: False,
) -> dict[str, int]:
    """Page through one Snapshot space; resumable via backfill_cursor {space, skip}.

    Commits after every proposal and saves the cursor after every page — same
    crash-safety contract as worker/kb.py's Discourse backfill: a SIGTERM or
    crash costs at most one page of re-listing, never re-crawling.
    """
    space = forum.backfill_cursor.get("space")
    if not space:
        raise ValueError(
            f"{forum.forum_slug}: backfill_cursor has no 'space' — check the kb.forums seed"
        )

    skip = int(forum.backfill_cursor.get("skip", 0))
    stats = {"topics_crawled": 0, "pages_listed": 0}

    for _ in range(LISTING_SAFETY_PAGES):
        if should_stop() or (max_topics and stats["topics_crawled"] >= max_topics):
            _save_cursor(conn, forum, {"space": space, "skip": skip})
            return stats

        proposals = _run_query(
            client, QUERY_PAGE, {"space": [space], "first": PAGE_SIZE, "skip": skip}
        )
        stats["pages_listed"] += 1
        if not proposals:
            break

        for proposal in proposals:
            if should_stop() or (max_topics and stats["topics_crawled"] >= max_topics):
                _save_cursor(conn, forum, {"space": space, "skip": skip})
                return stats
            _upsert_proposal(conn, forum, proposal)
            stats["topics_crawled"] += 1
            conn.commit()

        skip += len(proposals)
        _save_cursor(conn, forum, {"space": space, "skip": skip})

        if len(proposals) < PAGE_SIZE:
            break  # остання сторінка простору

    # backfill_cursor тримає ЛИШЕ {"space": ...} — на відміну від Discourse
    # (де cursor скидається у '{}', бо ідентичність форуму — це base_url),
    # тут space-id більше НІДЕ не зберігається, і incremental() читає його
    # звідси на кожному прогоні. 'skip' після завершення більше не потрібен.
    conn.execute(
        "UPDATE kb.forums SET backfill_done = true, backfill_cursor = %s, "
        "consecutive_failures = 0 WHERE id = %s",
        (json.dumps({"space": space}), forum.id),
    )
    conn.commit()
    log.info("%s backfill COMPLETE: %s", forum.forum_slug, stats)
    return stats


def incremental(conn: psycopg.Connection, client: HttpClient, forum: Forum) -> dict[str, int]:
    """New proposals since the watermark, plus a refreshed footer for any
    proposal returned again (still-open proposals change scores/state)."""
    space = forum.backfill_cursor.get("space")
    if not space:
        raise ValueError(
            f"{forum.forum_slug}: backfill_cursor has no 'space' — check the kb.forums seed"
        )

    if forum.last_post_seen_at:
        since_epoch = int(forum.last_post_seen_at.timestamp())
    else:
        # Перший inkremental одразу після backfill: watermark ще не виставлений
        # (kb.py's incremental так само не оновлює last_post_seen_at сам
        # backfill), тож підстрахуємось максимумом created_at, що вже архівовано.
        row = conn.execute(
            "SELECT max(created_at) AS newest FROM kb.topics WHERE forum_slug = %s",
            (forum.forum_slug,),
        ).fetchone()
        newest = row["newest"] if row else None
        since_epoch = int(newest.timestamp()) if newest else 0

    stats = {"topics_crawled": 0}
    newest_seen = forum.last_post_seen_at
    skip = 0

    for _ in range(INCREMENTAL_SAFETY_PAGES):
        proposals = _run_query(
            client,
            QUERY_SINCE,
            {"space": [space], "createdGt": since_epoch, "first": INCREMENTAL_PAGE_SIZE, "skip": skip},
        )
        if not proposals:
            break

        for proposal in proposals:
            _upsert_proposal(conn, forum, proposal)
            stats["topics_crawled"] += 1
            conn.commit()
            created = _epoch_to_dt(proposal.get("created"))
            if created and (newest_seen is None or created > newest_seen):
                newest_seen = created

        skip += len(proposals)
        if len(proposals) < INCREMENTAL_PAGE_SIZE:
            break

    # Watermark advances unconditionally: unlike Discourse's incremental
    # (which withholds it on ANY failed topic because a partial post fetch can
    # leave a topic half-stored), a Snapshot proposal is one atomic upsert —
    # either _upsert_proposal fully committed or the exception propagated
    # before this line ran at all, so there is no partial-topic state to protect.
    conn.execute(
        "UPDATE kb.forums SET last_post_seen_at = COALESCE(%s, last_post_seen_at), "
        "last_incremental_at = now(), consecutive_failures = 0 WHERE id = %s",
        (newest_seen, forum.id),
    )
    conn.commit()
    return stats
