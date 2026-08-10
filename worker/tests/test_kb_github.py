"""Тести worker/kb_github.py: token-skip, discussions/issues mapping
(body→#1, comments+replies flattening, category_name), пагінація зовнішньої
сторінки (discussions/issues) і вкладеної сторінки коментарів, GraphQL
errors-in-200, incremental watermark.

Без БД і без мережі: HttpClient підмінений легким фейком з .post_json()
(черга канонічних GraphQL-відповідей, той самий стиль, що й
worker/tests/test_kb_snapshot.py::_FakeHttpClient), psycopg-з'єднання —
фейком, що записує (sql, params) у список і повертає {"id": ...} на
RETURNING id — той самий _FakeConn-стиль."""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import datetime, timezone

import pytest

from worker import kb, kb_github
from worker.http import FetchError, SourceBlocked

REPO_DISCUSSIONS = "filecoin-project/community"
REPO_ISSUES = "ethereum-optimism/ecosystem-contributions"


# ── fixtures & fakes ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_module_state(monkeypatch):
    """Кожен тест стартує з чистим "once per process" прапорцем і зі
    справжнім (непорожнім) токеном за замовчуванням — Config заморожений
    dataclass, тож підміняємо саме прив'язку kb_github.config, а не
    атрибут на самому екземплярі (dataclasses.replace() дає копію)."""
    kb_github._warned_no_token = False
    monkeypatch.setattr(kb_github, "config", dataclasses.replace(kb_github.config, github_token="ghp_test_token"))
    yield


def _no_token(monkeypatch) -> None:
    monkeypatch.setattr(kb_github, "config", dataclasses.replace(kb_github.config, github_token=""))


def _forum(**overrides) -> kb.Forum:
    fields = dict(
        id=9,
        forum_slug="gh-filecoin",
        base_url="https://github.com/filecoin-project/community",
        kind="github",
        category_ids=None,
        backfill_done=False,
        backfill_cursor={"repo": REPO_DISCUSSIONS, "mode": "discussions"},
        last_post_seen_at=None,
    )
    fields.update(overrides)
    return kb.Forum(**fields)


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHttpClient:
    """Черга GraphQL-відповідей — по одній на кожен post_json(), у тому
    порядку, у якому модуль їх насправді запитує (зовнішня сторінка,
    потім, за потреби, дозавантаження comments/replies)."""

    def __init__(self, pages: list[dict]):
        self.pages = list(pages)
        self.calls: list[tuple] = []

    def post_json(self, url, payload, headers=None):
        self.calls.append((url, payload, headers))
        if not self.pages:
            return _FakeResponse({"data": {}})
        return _FakeResponse(self.pages.pop(0))


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, newest_bumped_at=None):
        self.calls: list[tuple] = []
        self._next_id = 500
        self._newest_bumped_at = newest_bumped_at

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "RETURNING id" in sql:
            row = {"id": self._next_id}
            self._next_id += 1
            return _FakeCursor(row)
        if "max(bumped_at)" in sql:
            return _FakeCursor({"newest": self._newest_bumped_at})
        return _FakeCursor(None)

    def commit(self):
        pass


def _post_insert_calls(conn: _FakeConn) -> list[tuple]:
    """conn.calls also carries the cursor-save UPDATEs backfill()/incremental()
    issue between/after topics — filter down to just the kb.posts inserts."""
    return [(sql, params) for sql, params in conn.calls if "INSERT INTO kb.posts" in sql]


# ── canned GraphQL node builders ───────────────────────────────────────


def _empty_conn(has_next=False, cursor=None):
    return {"pageInfo": {"hasNextPage": has_next, "endCursor": cursor}, "nodes": []}


def _reply(body="a reply", author="carol", created="2026-01-01T02:00:00Z"):
    return {"body": body, "url": "https://x/reply", "createdAt": created, "author": {"login": author}}


def _comment(id_="C1", body="a comment", author="bob", created="2026-01-01T01:00:00Z", replies=None):
    return {
        "id": id_,
        "body": body,
        "url": f"https://x/{id_}",
        "createdAt": created,
        "author": {"login": author},
        "replies": replies if replies is not None else _empty_conn(),
    }


def _discussion_node(number=1, comments=None, **overrides):
    node = {
        "number": number,
        "title": "Fund the thing",
        "body": "Please fund this.",
        "url": f"https://github.com/{REPO_DISCUSSIONS}/discussions/{number}",
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-02T00:00:00Z",
        "author": {"login": "alice"},
        "category": {"name": "RFPs"},
        "comments": comments if comments is not None else _empty_conn(),
    }
    node.update(overrides)
    return node


def _issue_node(number=1, comments=None, labels=("Foundation Mission Request", "funded"), **overrides):
    node = {
        "number": number,
        "title": "Grant request: build a thing",
        "body": "Requesting a grant.",
        "url": f"https://github.com/{REPO_ISSUES}/issues/{number}",
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-02T00:00:00Z",
        "author": {"login": "dave"},
        "labels": {"nodes": [{"name": n} for n in labels]},
        "comments": comments if comments is not None else _empty_conn(),
    }
    node.update(overrides)
    return node


def _listing_page(nodes, connection_key, has_next=False, cursor=None):
    return {
        "data": {
            "repository": {
                connection_key: {
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    "nodes": nodes,
                }
            }
        }
    }


# ── token-missing skip ──────────────────────────────────────────────


def test_backfill_skips_without_token_and_touches_neither_db_nor_network(monkeypatch):
    _no_token(monkeypatch)
    forum = _forum()
    conn = _FakeConn()
    client = _FakeHttpClient([])

    stats = kb_github.backfill(conn, client, forum)

    assert stats["topics_crawled"] == 0
    assert conn.calls == []
    assert client.calls == []


def test_incremental_skips_without_token_and_touches_neither_db_nor_network(monkeypatch):
    _no_token(monkeypatch)
    forum = _forum(backfill_done=True)
    conn = _FakeConn()
    client = _FakeHttpClient([])

    stats = kb_github.incremental(conn, client, forum)

    assert stats["topics_crawled"] == 0
    assert conn.calls == []
    assert client.calls == []


def test_no_token_warning_logs_once_per_process_across_multiple_forums(monkeypatch, caplog):
    _no_token(monkeypatch)
    caplog.set_level(logging.WARNING)

    kb_github.backfill(_FakeConn(), _FakeHttpClient([]), _forum(forum_slug="gh-filecoin"))
    kb_github.backfill(_FakeConn(), _FakeHttpClient([]), _forum(forum_slug="gh-metaplex"))

    warnings = [r for r in caplog.records if "GITHUB_TOKEN" in r.message]
    assert len(warnings) == 1


def test_backfill_missing_repo_or_mode_raises_value_error():
    forum = _forum(backfill_cursor={})
    with pytest.raises(ValueError):
        kb_github.backfill(_FakeConn(), _FakeHttpClient([]), forum)


def test_incremental_missing_repo_or_mode_raises_value_error():
    forum = _forum(backfill_cursor={}, backfill_done=True)
    with pytest.raises(ValueError):
        kb_github.incremental(_FakeConn(), _FakeHttpClient([]), forum)


# ── _category_name ──────────────────────────────────────────────────


def test_category_name_discussions_uses_category_field():
    node = _discussion_node()
    assert kb_github._category_name(node, "discussions") == "RFPs"


def test_category_name_issues_joins_label_names():
    node = _issue_node()
    assert kb_github._category_name(node, "issues") == "Foundation Mission Request, funded"


def test_category_name_issues_none_when_no_labels():
    node = _issue_node(labels=())
    assert kb_github._category_name(node, "issues") is None


# ── discussions mapping: body→#1, comments+replies flattening order ──


def test_backfill_discussions_maps_body_then_comments_with_replies_flattened_in_order():
    forum = _forum()
    conn = _FakeConn()
    comment1 = _comment(id_="C1", body="first comment", replies=_empty_conn())
    comment1["replies"] = {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [_reply(body="reply to first")],
    }
    comment2 = _comment(id_="C2", body="second comment")  # no replies
    node = _discussion_node(
        number=42,
        comments={"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [comment1, comment2]},
    )
    page = _listing_page([node], "discussions")
    client = _FakeHttpClient([page])

    stats = kb_github.backfill(conn, client, forum)

    assert stats["topics_crawled"] == 1
    assert stats["posts_stored"] == 4  # body + comment1 + reply + comment2

    topic_sql, topic_params = conn.calls[0]
    assert "ON CONFLICT (forum_slug, topic_id) DO UPDATE" in topic_sql
    assert topic_params[0] == "gh-filecoin"
    assert topic_params[1] == "42"
    assert isinstance(topic_params[1], str)  # topic_id — text (migration 008)
    assert topic_params[2] == "RFPs"  # category_name

    post_calls = _post_insert_calls(conn)
    assert len(post_calls) == 4
    texts_in_order = [params[-1] for _, params in post_calls]
    assert texts_in_order == ["Please fund this.", "first comment", "reply to first", "second comment"]

    # post_number is params[1] per _upsert_post's param order
    post_numbers = [params[1] for _, params in post_calls]
    assert post_numbers == [1, 2, 3, 4]


def test_backfill_discussions_issues_graphql_with_asc_direction_for_backfill():
    forum = _forum()
    client = _FakeHttpClient([_listing_page([], "discussions")])

    kb_github.backfill(_FakeConn(), client, forum)

    _, payload, headers = client.calls[0]
    assert payload["variables"]["dir"] == "ASC"
    assert payload["variables"]["owner"] == "filecoin-project"
    assert payload["variables"]["name"] == "community"
    assert headers == {"Authorization": "bearer ghp_test_token"}


# ── issues mapping: labels→category_name, no replies level ───────────


def test_backfill_issues_maps_labels_to_category_name_and_flattens_comments_without_replies():
    forum = _forum(
        forum_slug="gh-op-missions",
        base_url="https://github.com/ethereum-optimism/ecosystem-contributions",
        backfill_cursor={"repo": REPO_ISSUES, "mode": "issues"},
    )
    conn = _FakeConn()
    comment = {
        "id": "IC1",
        "body": "issue comment",
        "url": "https://x/ic1",
        "createdAt": "2026-01-01T01:00:00Z",
        "author": {"login": "bob"},
        # No "replies" key at all — IssueComment has no such field.
    }
    node = _issue_node(
        number=7,
        comments={"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [comment]},
    )
    page = _listing_page([node], "issues")
    client = _FakeHttpClient([page])

    stats = kb_github.backfill(conn, client, forum)

    assert stats["topics_crawled"] == 1
    assert stats["posts_stored"] == 2  # body + one comment, no replies level

    topic_sql, topic_params = conn.calls[0]
    assert topic_params[2] == "Foundation Mission Request, funded"

    post_texts = [params[-1] for _, params in _post_insert_calls(conn)]
    assert post_texts == ["Requesting a grant.", "issue comment"]


# ── outer-page pagination: cursor advance + resume ────────────────────


def test_backfill_pages_outer_connection_and_saves_after_cursor_between_pages():
    forum = _forum()
    conn = _FakeConn()
    page1 = _listing_page(
        [_discussion_node(number=1)], "discussions", has_next=True, cursor="CURSOR_1"
    )
    page2 = _listing_page([_discussion_node(number=2)], "discussions", has_next=False)
    client = _FakeHttpClient([page1, page2])

    stats = kb_github.backfill(conn, client, forum)

    assert stats["topics_crawled"] == 2
    assert stats["pages_listed"] == 2

    _, second_payload, _ = client.calls[1]
    assert second_payload["variables"]["after"] == "CURSOR_1"

    # Проміжний save (після сторінки 1) тримає {repo, mode, after}.
    intermediate = [
        json.loads(params[0])
        for sql, params in conn.calls
        if "UPDATE kb.forums SET backfill_cursor" in sql
    ]
    assert {"repo": REPO_DISCUSSIONS, "mode": "discussions", "after": "CURSOR_1"} in intermediate

    # Завершення прибирає 'after' — лишається лише {repo, mode}.
    last_sql, last_params = conn.calls[-1]
    assert "backfill_done = true" in last_sql
    assert json.loads(last_params[0]) == {"repo": REPO_DISCUSSIONS, "mode": "discussions"}


def test_backfill_resumes_from_the_saved_after_cursor():
    forum = _forum(backfill_cursor={"repo": REPO_DISCUSSIONS, "mode": "discussions", "after": "RESUME_HERE"})
    client = _FakeHttpClient([_listing_page([], "discussions")])

    kb_github.backfill(_FakeConn(), client, forum)

    _, payload, _ = client.calls[0]
    assert payload["variables"]["after"] == "RESUME_HERE"


def test_backfill_honors_max_topics_and_should_stop():
    forum = _forum()
    nodes = [_discussion_node(number=i) for i in range(5)]
    client = _FakeHttpClient([_listing_page(nodes, "discussions")])

    stats = kb_github.backfill(_FakeConn(), client, forum, max_topics=2)

    assert stats["topics_crawled"] == 2


# ── nested comment pagination beyond first page ───────────────────────


def test_walk_comments_paginates_beyond_the_first_embedded_page():
    forum = _forum()
    conn = _FakeConn()
    comment1 = _comment(id_="C1", body="comment one")
    comment2 = _comment(id_="C2", body="comment two")
    comment3 = _comment(id_="C3", body="comment three")

    node = _discussion_node(
        number=9,
        comments={
            "pageInfo": {"hasNextPage": True, "endCursor": "COMMENTS_CURSOR"},
            "nodes": [comment1, comment2],
        },
    )
    outer_page = _listing_page([node], "discussions", has_next=False)
    # Дозавантаження comments для discussion #9: одна ще сторінка, остання.
    more_comments = {
        "data": {
            "repository": {
                "discussion": {
                    "comments": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [comment3],
                    }
                }
            }
        }
    }
    client = _FakeHttpClient([outer_page, more_comments])

    stats = kb_github.backfill(conn, client, forum)

    assert stats["topics_crawled"] == 1
    assert stats["posts_stored"] == 4  # body + 3 comments, none silently dropped
    assert len(client.calls) == 2

    _, cont_payload, _ = client.calls[1]
    assert cont_payload["variables"]["number"] == 9
    assert cont_payload["variables"]["after"] == "COMMENTS_CURSOR"

    post_texts = [params[-1] for _, params in _post_insert_calls(conn)]
    assert post_texts == ["Please fund this.", "comment one", "comment two", "comment three"]


def test_walk_replies_paginates_beyond_the_first_embedded_page():
    forum = _forum()
    conn = _FakeConn()
    comment = _comment(
        id_="C1",
        body="popular comment",
        replies={"pageInfo": {"hasNextPage": True, "endCursor": "REPLIES_CURSOR"}, "nodes": [_reply(body="reply one")]},
    )
    node = _discussion_node(
        number=3,
        comments={"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [comment]},
    )
    outer_page = _listing_page([node], "discussions")
    more_replies = {
        "data": {
            "node": {
                "replies": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [_reply(body="reply two")],
                }
            }
        }
    }
    client = _FakeHttpClient([outer_page, more_replies])

    stats = kb_github.backfill(conn, client, forum)

    assert stats["posts_stored"] == 4  # body + comment + reply one + reply two
    _, cont_payload, _ = client.calls[1]
    assert cont_payload["variables"]["id"] == "C1"
    assert cont_payload["variables"]["after"] == "REPLIES_CURSOR"

    post_texts = [params[-1] for _, params in _post_insert_calls(conn)]
    assert post_texts == ["Please fund this.", "popular comment", "reply one", "reply two"]


# ── GraphQL errors-in-200 ─────────────────────────────────────────────


def test_post_raises_fetch_error_on_graphql_errors_in_200():
    client = _FakeHttpClient([{"errors": [{"message": "boom"}]}])
    with pytest.raises(FetchError):
        kb_github._post(client, {}, kb_github.QUERY_DISCUSSIONS_PAGE, {})


def test_backfill_propagates_graphql_errors_as_fetch_error():
    forum = _forum()
    client = _FakeHttpClient([{"errors": [{"type": "NOT_FOUND", "message": "Could not resolve"}]}])
    with pytest.raises(FetchError):
        kb_github.backfill(_FakeConn(), client, forum)


class _RaisingClient:
    """post_json() raises, to exercise _post's 401/403 log-and-reraise path."""

    def __init__(self, exc):
        self._exc = exc

    def post_json(self, url, payload, headers=None):
        raise self._exc


def test_post_logs_and_reraises_on_401(caplog):
    caplog.set_level(logging.ERROR)
    client = _RaisingClient(FetchError("POST https://api.github.com/graphql returned 401"))

    with pytest.raises(FetchError):
        kb_github._post(client, {}, kb_github.QUERY_DISCUSSIONS_PAGE, {})

    assert any("GITHUB_TOKEN" in r.message for r in caplog.records)


def test_post_logs_and_reraises_on_403_source_blocked(caplog):
    caplog.set_level(logging.ERROR)
    client = _RaisingClient(SourceBlocked("POST https://api.github.com/graphql returned 403"))

    with pytest.raises(SourceBlocked):
        kb_github._post(client, {}, kb_github.QUERY_DISCUSSIONS_PAGE, {})

    assert any("GITHUB_TOKEN" in r.message for r in caplog.records)


# ── incremental: watermark stop + fallback + pagination ───────────────


def test_incremental_stops_at_watermark_and_only_processes_newer_items():
    watermark = datetime(2026, 1, 1, tzinfo=timezone.utc)
    forum = _forum(
        backfill_done=True,
        backfill_cursor={"repo": REPO_DISCUSSIONS, "mode": "discussions", "since": watermark.isoformat()},
    )
    conn = _FakeConn()
    newer = _discussion_node(number=100, updatedAt="2026-02-01T00:00:00Z")
    older = _discussion_node(number=1, updatedAt="2025-12-01T00:00:00Z")  # at/behind watermark
    page = _listing_page([newer, older], "discussions", has_next=True, cursor="SHOULD_NOT_BE_FOLLOWED")
    client = _FakeHttpClient([page])

    stats = kb_github.incremental(conn, client, forum)

    assert stats["topics_crawled"] == 1
    assert len(client.calls) == 1  # stopped client-side; never followed the next-page cursor

    _, payload, _ = client.calls[0]
    assert payload["variables"]["dir"] == "DESC"

    last_sql, last_params = conn.calls[-1]
    assert "backfill_cursor" in last_sql
    saved_cursor = json.loads(last_params[0])
    assert saved_cursor["since"] == "2026-02-01T00:00:00+00:00"


def test_incremental_falls_back_to_max_bumped_at_when_no_since_cursor_yet():
    fallback = datetime(2025, 6, 1, tzinfo=timezone.utc)
    forum = _forum(backfill_done=True)  # cursor has repo+mode but no 'since'
    conn = _FakeConn(newest_bumped_at=fallback)
    older = _discussion_node(number=1, updatedAt="2025-01-01T00:00:00Z")
    client = _FakeHttpClient([_listing_page([older], "discussions")])

    stats = kb_github.incremental(conn, client, forum)

    assert stats["topics_crawled"] == 0  # older than the fallback watermark


def test_incremental_paginates_when_first_page_is_entirely_newer_than_watermark():
    watermark = datetime(2026, 1, 1, tzinfo=timezone.utc)
    forum = _forum(
        backfill_done=True,
        backfill_cursor={"repo": REPO_DISCUSSIONS, "mode": "discussions", "since": watermark.isoformat()},
    )
    conn = _FakeConn()
    page1 = _listing_page(
        [_discussion_node(number=2, updatedAt="2026-03-01T00:00:00Z")],
        "discussions", has_next=True, cursor="NEXT",
    )
    page2 = _listing_page(
        [_discussion_node(number=1, updatedAt="2026-02-01T00:00:00Z")],
        "discussions", has_next=False,
    )
    client = _FakeHttpClient([page1, page2])

    stats = kb_github.incremental(conn, client, forum)

    assert stats["topics_crawled"] == 2
    assert len(client.calls) == 2
    _, second_payload, _ = client.calls[1]
    assert second_payload["variables"]["after"] == "NEXT"


def test_incremental_missing_repo_or_mode_still_raises_with_token_present():
    forum = _forum(backfill_cursor={"mode": "discussions"}, backfill_done=True)  # no 'repo'
    with pytest.raises(ValueError):
        kb_github.incremental(_FakeConn(), _FakeHttpClient([]), forum)
