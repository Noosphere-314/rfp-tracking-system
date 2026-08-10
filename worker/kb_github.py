"""Knowledge-base crawler for kb.forums.kind = 'github' (KB-Module-Design.md
extension, migration 010).

Same backfill(conn, client, forum, ...) / incremental(conn, client, forum)
contract as worker/kb.py's Discourse crawler and worker/kb_snapshot.py's
Snapshot crawler, on purpose: worker/main.py's KB_CRAWLERS registry
(mirroring worker/fetchers/__init__.py's FETCHERS) dispatches on
kb.forums.kind without special-casing any of the three. `client` is the same
shared, SSRF-checked, DB-backed-rate-limited HttpClient the pipeline
fetchers use (worker/http.py, worker/ratelimit.py) — every GraphQL POST to
api.github.com goes through client.post_json(), which calls
ratelimit.acquire() first, so "one page per ~1s" politeness comes for free
from the same shared per-host token bucket worker/fetchers/github_discussions.py
already relies on; there is no separate sleep() in this module.

Without GITHUB_TOKEN configured: unlike worker/fetchers/github_discussions.py
(a pipeline source with no token is a real misconfiguration and fails loudly),
a KB forum here is a known, tolerated state until ops adds the PAT — we log
one warning and return zero-stats WITHOUT touching consecutive_failures. The
warning fires at most once per process (module-level flag): the three
gh-* rows in one `kb-backfill`/`kb-update` run would otherwise print the
same line three times.

One kb.forums row = one GitHub repo, read via backfill_cursor->>'repo' and
->>'mode' (set once by migration 010's seed and never cleared, same pattern
as kb_snapshot.py's ->>'space'). Two shapes share this module because a
"grants" repo is sometimes GH Discussions and sometimes plain Issues:

  mode="discussions"  repository.discussions — nodes carry `category { name }`
                       and a `comments` connection whose comments in turn
                       carry a `replies` connection (two nesting levels)
  mode="issues"        repository.issues — nodes carry `labels` (joined into
                       category_name) and a flat `comments` connection only
                       (IssueComment has no `replies` field at all)

Mapping, either mode: discussion/issue → kb.topics (topic_id=str(number));
body → kb.posts post_number=1; every comment, with its own replies flattened
immediately after it (chronological within each level, matching the sibling
Discourse's post-stream shape) → kb.posts post_number=2... Upserts are
idempotent on (forum_slug, topic_id) / (topic_ref, post_number): a re-crawl
refreshes title/bumped_at/category_name and appends whatever is new.

Both the outer discussions/issues connection AND each thread's `comments`
connection (and each comment's `replies` connection) paginate past their
first page instead of truncating — a Filecoin thread can run to 326
comments, well past the 50-per-page GraphQL default. A NESTED_SAFETY_PAGES
runaway-loop breaker exists only as a last-resort guard (mirrors
LISTING_SAFETY_PAGES in kb.py/kb_snapshot.py); tripping it is logged loudly
because, unlike the outer listing cursor, there is nowhere to resume a
truncated comment thread from on the next run.

Cursor shape: {"repo", "mode", "after"} while backfill is in progress
("after" = the outer connection's endCursor, resumable — dropped once
backfill_done flips true, same as kb_snapshot.py drops "skip"); {"repo",
"mode", "since"} for the steady state incremental() reads and writes
("since" = the newest updatedAt archived so far). GitHub's discussions/issues
connections have no server-side "updated since X" filter (unlike Snapshot's
`created_gt`), so incremental() pages DESC by updatedAt and stops client-side
at the first item at-or-behind the watermark instead.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import psycopg

from .config import config
from .http import FetchError, HttpClient, SourceBlocked
from .kb import Forum

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.github.com/graphql"

PAGE_SIZE = 25                  # outer discussions/issues connection page
COMMENTS_PAGE_SIZE = 50         # nested comments page, both modes
REPLIES_PAGE_SIZE = 50          # nested replies page, discussions only
LISTING_SAFETY_PAGES = 500      # runaway-loop breaker, mirrors kb_snapshot.py
INCREMENTAL_SAFETY_PAGES = 50   # ditto, scaled down for the hourly path
NESTED_SAFETY_PAGES = 20        # guards the inner comment/reply pagination loops

# Порожній рядок і None рівнозначні "токена нема" — config._env() уже стрипає
# пробіли, тож перевірки нижче просто на falsy-значення.
_warned_no_token = False


def _warn_no_token() -> None:
    """Один раз за процес: кожен виклик `kb-backfill`/`kb-update` — окремий
    процес (worker/main.py cmd_kb_backfill/cmd_kb_update), тож простий
    модульний прапорець і дає "один раз за прогін", без координації з
    main.py (яку чіпати не можна) через три gh-* рядки в одному прогоні."""
    global _warned_no_token
    if not _warned_no_token:
        log.warning("gh-*: GITHUB_TOKEN не заданий — пропускаю (це не збій)")
        _warned_no_token = True


QUERY_DISCUSSIONS_PAGE = """
query DiscussionsPage($owner: String!, $name: String!, $first: Int!, $after: String, $dir: OrderDirection!) {
  repository(owner: $owner, name: $name) {
    discussions(first: $first, after: $after, orderBy: {field: UPDATED_AT, direction: $dir}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        body
        url
        createdAt
        updatedAt
        author { login }
        category { name }
        comments(first: %d) {
          pageInfo { hasNextPage endCursor }
          nodes {
            id
            body
            url
            createdAt
            author { login }
            replies(first: %d) {
              pageInfo { hasNextPage endCursor }
              nodes { body url createdAt author { login } }
            }
          }
        }
      }
    }
  }
}
""" % (COMMENTS_PAGE_SIZE, REPLIES_PAGE_SIZE)

QUERY_ISSUES_PAGE = """
query IssuesPage($owner: String!, $name: String!, $first: Int!, $after: String, $dir: OrderDirection!) {
  repository(owner: $owner, name: $name) {
    issues(first: $first, after: $after, orderBy: {field: UPDATED_AT, direction: $dir}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        body
        url
        createdAt
        updatedAt
        author { login }
        labels(first: 20) { nodes { name } }
        comments(first: %d) {
          pageInfo { hasNextPage endCursor }
          nodes {
            id
            body
            url
            createdAt
            author { login }
          }
        }
      }
    }
  }
}
""" % COMMENTS_PAGE_SIZE

# Одна конкретна дискусія/issue за номером — для дозавантаження comments понад
# ту сторінку, що вже прийшла разом з лістингом (QUERY_*_PAGE вище).
QUERY_DISCUSSION_COMMENTS_PAGE = """
query DiscussionCommentsPage($owner: String!, $name: String!, $number: Int!, $first: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    discussion(number: $number) {
      comments(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          body
          url
          createdAt
          author { login }
          replies(first: %d) {
            pageInfo { hasNextPage endCursor }
            nodes { body url createdAt author { login } }
          }
        }
      }
    }
  }
}
""" % REPLIES_PAGE_SIZE

QUERY_ISSUE_COMMENTS_PAGE = """
query IssueCommentsPage($owner: String!, $name: String!, $number: Int!, $first: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      comments(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          body
          url
          createdAt
          author { login }
        }
      }
    }
  }
}
"""

# Дозавантаження replies понад першу сторінку одного коментаря. DiscussionComment
# реалізує GraphQL-інтерфейс Node — саме тому це можливо по голому `id`, без
# знання номера дискусії чи позиції коментаря в списку.
QUERY_REPLIES_PAGE = """
query CommentRepliesPage($id: ID!, $first: Int!, $after: String) {
  node(id: $id) {
    ... on DiscussionComment {
      replies(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { body url createdAt author { login } }
      }
    }
  }
}
"""


def _headers() -> dict[str, str]:
    return {"Authorization": f"bearer {config.github_token}"}


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _coerce_dt(value: Any) -> datetime | None:
    """cursor->>'since' comes back as str (jsonb), a fallback SQL MAX() comes
    back as datetime — accept either."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return _ts(value)


def _cursor(repo: str, mode: str, *, after: str | None = None, since: str | None = None) -> dict:
    cursor: dict[str, str] = {"repo": repo, "mode": mode}
    if after:
        cursor["after"] = after
    if since:
        cursor["since"] = since
    return cursor


def _save_cursor(conn: psycopg.Connection, forum: Forum, cursor: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE kb.forums SET backfill_cursor = %s WHERE id = %s",
        (json.dumps(cursor), forum.id),
    )
    conn.commit()


def _post(client: HttpClient, headers: dict[str, str], query: str, variables: dict[str, Any]) -> dict:
    """One GraphQL POST. Raises on GraphQL errors-in-200 (GitHub's habit of
    returning HTTP 200 with a populated `errors` array — a plain status-code
    check would miss it completely) and adds a clear log line for 401/403
    (bad/expired/missing-scope token) before re-raising the original
    exception type unchanged, so main.py's SourceBlocked-vs-Exception
    handling in cmd_kb_backfill still applies."""
    try:
        response = client.post_json(GRAPHQL_URL, {"query": query, "variables": variables}, headers=headers)
    except (SourceBlocked, FetchError) as exc:
        message = str(exc)
        if "401" in message or "403" in message:
            log.error("gh: GraphQL auth failed — перевір GITHUB_TOKEN (права/термін дії): %s", message)
        raise
    payload = response.json()
    if payload.get("errors"):
        raise FetchError(f"github graphql errors: {payload['errors']}")
    return payload.get("data") or {}


def _fetch_topics_page(
    client: HttpClient, headers: dict[str, str], owner: str, name: str,
    mode: str, first: int, after: str | None, direction: str,
) -> tuple[list[dict], dict]:
    query = QUERY_DISCUSSIONS_PAGE if mode == "discussions" else QUERY_ISSUES_PAGE
    data = _post(client, headers, query, {
        "owner": owner, "name": name, "first": first, "after": after, "dir": direction,
    })
    repository = data.get("repository") or {}
    connection = repository.get("discussions" if mode == "discussions" else "issues") or {}
    nodes = connection.get("nodes") or []
    page_info = connection.get("pageInfo") or {"hasNextPage": False, "endCursor": None}
    return nodes, page_info


def _fetch_more_comments(
    client: HttpClient, headers: dict[str, str], owner: str, name: str,
    number: int, mode: str, after: str | None,
) -> tuple[list[dict], dict]:
    query = QUERY_DISCUSSION_COMMENTS_PAGE if mode == "discussions" else QUERY_ISSUE_COMMENTS_PAGE
    data = _post(client, headers, query, {
        "owner": owner, "name": name, "number": number, "first": COMMENTS_PAGE_SIZE, "after": after,
    })
    repository = data.get("repository") or {}
    thread = repository.get("discussion" if mode == "discussions" else "issue") or {}
    comments_conn = thread.get("comments") or {}
    return (
        list(comments_conn.get("nodes") or []),
        comments_conn.get("pageInfo") or {"hasNextPage": False, "endCursor": None},
    )


def _fetch_more_replies(
    client: HttpClient, headers: dict[str, str], comment_id: str, after: str | None,
) -> tuple[list[dict], dict]:
    data = _post(client, headers, QUERY_REPLIES_PAGE, {
        "id": comment_id, "first": REPLIES_PAGE_SIZE, "after": after,
    })
    node = data.get("node") or {}
    replies_conn = node.get("replies") or {}
    return (
        list(replies_conn.get("nodes") or []),
        replies_conn.get("pageInfo") or {"hasNextPage": False, "endCursor": None},
    )


def _comment_to_post(node: dict) -> dict[str, Any]:
    return {
        "author": (node.get("author") or {}).get("login"),
        "posted_at": _ts(node.get("createdAt")),
        "text": node.get("body") or "",
    }


def _walk_replies(
    client: HttpClient, headers: dict[str, str], owner: str, name: str, comment: dict,
) -> list[dict]:
    replies_conn = comment.get("replies")
    if not replies_conn:
        return []  # issue comments мають нема поля `replies` взагалі

    posts = [_comment_to_post(reply) for reply in (replies_conn.get("nodes") or [])]
    page_info = replies_conn.get("pageInfo") or {"hasNextPage": False, "endCursor": None}
    comment_id = comment.get("id")
    pages = 0

    while page_info.get("hasNextPage") and comment_id:
        pages += 1
        if pages > NESTED_SAFETY_PAGES:
            log.warning(
                "gh %s/%s: reply thread on comment %s capped at safety limit "
                "(%d pages) — some replies were NOT archived",
                owner, name, comment.get("url"), NESTED_SAFETY_PAGES,
            )
            break
        more, page_info = _fetch_more_replies(client, headers, comment_id, page_info.get("endCursor"))
        posts.extend(_comment_to_post(reply) for reply in more)

    return posts


def _walk_comments(
    client: HttpClient, headers: dict[str, str], owner: str, name: str,
    mode: str, number: int, node: dict,
) -> list[dict]:
    """Full comment thread for one discussion/issue: every comment, replies
    (if any) flattened right after their own parent, chronological within
    each level. Paginates past the first embedded page of comments — and
    each comment's replies past their own first page — instead of silently
    truncating at COMMENTS_PAGE_SIZE/REPLIES_PAGE_SIZE."""
    posts: list[dict[str, Any]] = []
    comments_conn = node.get("comments") or {}
    comments = list(comments_conn.get("nodes") or [])
    page_info = comments_conn.get("pageInfo") or {"hasNextPage": False, "endCursor": None}
    pages = 0

    while True:
        for comment in comments:
            posts.append(_comment_to_post(comment))
            posts.extend(_walk_replies(client, headers, owner, name, comment))

        if not page_info.get("hasNextPage"):
            break
        pages += 1
        if pages > NESTED_SAFETY_PAGES:
            log.warning(
                "gh %s/%s#%s: comments capped at safety limit (%d pages) — "
                "some comments were NOT archived",
                owner, name, number, NESTED_SAFETY_PAGES,
            )
            break
        comments, page_info = _fetch_more_comments(
            client, headers, owner, name, number, mode, page_info.get("endCursor")
        )

    return posts


def _category_name(node: dict, mode: str) -> str | None:
    if mode == "discussions":
        return (node.get("category") or {}).get("name")
    names = [
        label.get("name")
        for label in ((node.get("labels") or {}).get("nodes") or [])
        if label.get("name")
    ]
    return ", ".join(names) if names else None


def _upsert_post(conn: psycopg.Connection, topic_ref: int, post_number: int, post: dict) -> None:
    conn.execute(
        """
        INSERT INTO kb.posts (topic_ref, post_number, author, posted_at,
                              edited_at, raw_text, raw_html)
        VALUES (%s, %s, %s, %s, NULL, %s, NULL)
        ON CONFLICT (topic_ref, post_number) DO UPDATE
            SET raw_text = EXCLUDED.raw_text,
                author = EXCLUDED.author
        """,
        (topic_ref, post_number, post["author"], post["posted_at"], post["text"]),
    )


def _upsert_topic(
    conn: psycopg.Connection, forum: Forum, node: dict, mode: str, comment_posts: list[dict]
) -> None:
    """One discussion/issue → one kb.topics row + kb.posts rows #1..N.

    ON CONFLICT DO UPDATE refreshes title/category_name/bumped_at/post_count
    on every re-crawl — a still-open discussion picks up new comments and a
    relabelled issue picks up its new category_name, same as kb.py's
    Discourse _upsert_topic. topic_id is stored via str(): kb.topics.topic_id
    is `text` (migration 008), same reasoning as the sibling crawlers even
    though GitHub's own numbers are small ints — one shared column type,
    one shared comparison rule.
    """
    topic_id = str(node["number"])
    created_at = _ts(node.get("createdAt"))
    bumped_at = _ts(node.get("updatedAt"))
    category_name = _category_name(node, mode)
    author = (node.get("author") or {}).get("login")
    post_count = 1 + len(comment_posts)
    noun = "discussion" if mode == "discussions" else "issue"

    topic_row = conn.execute(
        """
        INSERT INTO kb.topics (forum_slug, topic_id, category_id, category_name,
                               title, url, author, created_at, bumped_at,
                               post_count, last_crawled_at)
        VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (forum_slug, topic_id) DO UPDATE
            SET title = EXCLUDED.title,
                category_name = EXCLUDED.category_name,
                bumped_at = EXCLUDED.bumped_at,
                post_count = EXCLUDED.post_count,
                last_crawled_at = now()
        RETURNING id
        """,
        (
            forum.forum_slug,
            topic_id,
            category_name,
            node.get("title") or f"{noun} {topic_id}",
            node.get("url") or "",
            author,
            created_at,
            bumped_at,
            post_count,
        ),
    ).fetchone()
    topic_ref = topic_row["id"]

    _upsert_post(conn, topic_ref, 1, {"author": author, "posted_at": created_at, "text": node.get("body") or ""})
    for offset, post in enumerate(comment_posts, start=2):
        _upsert_post(conn, topic_ref, offset, post)


def backfill(
    conn: psycopg.Connection,
    client: HttpClient,
    forum: Forum,
    max_topics: int | None = None,
    should_stop=lambda: False,
) -> dict[str, int]:
    """Page through one repo's discussions/issues; resumable via
    backfill_cursor {repo, mode, after}.

    Commits after every topic and saves the cursor after every page — same
    crash-safety contract as the sibling crawlers: a SIGTERM or crash costs
    at most one page of re-listing, never re-crawling.
    """
    if not config.github_token:
        _warn_no_token()
        return {"topics_crawled": 0, "posts_stored": 0, "pages_listed": 0}

    repo = forum.backfill_cursor.get("repo")
    mode = forum.backfill_cursor.get("mode")
    if not repo or not mode:
        raise ValueError(
            f"{forum.forum_slug}: backfill_cursor has no 'repo'/'mode' — check the kb.forums seed"
        )
    owner, name = repo.split("/", 1)
    headers = _headers()
    after = forum.backfill_cursor.get("after")
    stats = {"topics_crawled": 0, "posts_stored": 0, "pages_listed": 0}

    for _ in range(LISTING_SAFETY_PAGES):
        if should_stop() or (max_topics and stats["topics_crawled"] >= max_topics):
            _save_cursor(conn, forum, _cursor(repo, mode, after=after))
            return stats

        nodes, page_info = _fetch_topics_page(client, headers, owner, name, mode, PAGE_SIZE, after, "ASC")
        stats["pages_listed"] += 1
        if not nodes:
            break

        for node in nodes:
            if should_stop() or (max_topics and stats["topics_crawled"] >= max_topics):
                _save_cursor(conn, forum, _cursor(repo, mode, after=after))
                return stats
            comment_posts = _walk_comments(client, headers, owner, name, mode, node["number"], node)
            _upsert_topic(conn, forum, node, mode, comment_posts)
            stats["topics_crawled"] += 1
            stats["posts_stored"] += 1 + len(comment_posts)
            conn.commit()

        after = page_info.get("endCursor")
        _save_cursor(conn, forum, _cursor(repo, mode, after=after))
        if not page_info.get("hasNextPage"):
            break

    # Курсор по завершенні тримає лише {repo, mode} — 'after' більше не
    # потрібен, той самий підхід, що й kb_snapshot.py щодо 'skip'.
    conn.execute(
        "UPDATE kb.forums SET backfill_done = true, backfill_cursor = %s, "
        "consecutive_failures = 0 WHERE id = %s",
        (json.dumps({"repo": repo, "mode": mode}), forum.id),
    )
    conn.commit()
    log.info("%s backfill COMPLETE: %s", forum.forum_slug, stats)
    return stats


def incremental(conn: psycopg.Connection, client: HttpClient, forum: Forum) -> dict[str, int]:
    """New/updated discussions or issues since the watermark.

    Pages DESC by updatedAt and stops at the first item at-or-behind the
    watermark: GitHub's discussions/issues connections have no server-side
    "updated since" filter (unlike Snapshot's `created_gt`), so the stop
    condition has to be client-side.
    """
    if not config.github_token:
        _warn_no_token()
        return {"topics_crawled": 0, "posts_stored": 0}

    repo = forum.backfill_cursor.get("repo")
    mode = forum.backfill_cursor.get("mode")
    if not repo or not mode:
        raise ValueError(
            f"{forum.forum_slug}: backfill_cursor has no 'repo'/'mode' — check the kb.forums seed"
        )
    owner, name = repo.split("/", 1)
    headers = _headers()

    since_dt = _coerce_dt(forum.backfill_cursor.get("since"))
    if since_dt is None:
        # Перший incremental одразу після backfill: 'since' ще не виставлений
        # (backfill() ніколи його не пише), тож підстрахуємось найновішим
        # bumped_at, що вже архівовано — той самий трюк, що й kb_snapshot.py
        # робить через max(created_at).
        row = conn.execute(
            "SELECT max(bumped_at) AS newest FROM kb.topics WHERE forum_slug = %s",
            (forum.forum_slug,),
        ).fetchone()
        since_dt = _coerce_dt(row["newest"] if row else None)

    stats = {"topics_crawled": 0, "posts_stored": 0}
    newest_seen = since_dt
    after: str | None = None

    for _ in range(INCREMENTAL_SAFETY_PAGES):
        nodes, page_info = _fetch_topics_page(client, headers, owner, name, mode, PAGE_SIZE, after, "DESC")
        if not nodes:
            break

        stop = False
        for node in nodes:
            updated = _ts(node.get("updatedAt"))
            if since_dt and updated and updated <= since_dt:
                stop = True
                break
            comment_posts = _walk_comments(client, headers, owner, name, mode, node["number"], node)
            _upsert_topic(conn, forum, node, mode, comment_posts)
            stats["topics_crawled"] += 1
            stats["posts_stored"] += 1 + len(comment_posts)
            conn.commit()
            if updated and (newest_seen is None or updated > newest_seen):
                newest_seen = updated

        if stop or not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")

    since_value = newest_seen.isoformat() if newest_seen else forum.backfill_cursor.get("since")
    _save_cursor(conn, forum, _cursor(repo, mode, since=since_value))
    return stats
