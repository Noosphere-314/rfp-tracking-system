"""Тести worker/kb_snapshot.py: mapping proposal→topic/post, футер-рендеринг,
конвертація epoch, url-фолбек, і advance/resume курсора для backfill/incremental.

Без БД і без мережі: HttpClient підмінений легким фейком з .post_json(),
psycopg-з'єднання — фейком, що записує (sql, params) у список і повертає
{"id": ...} на RETURNING id, той самий стиль fakes+monkeypatch, що й в
admin/tests (наприклад admin/tests/test_briefs.py::_Conn)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from worker import kb, kb_snapshot
from worker.http import FetchError

PROPOSAL = {
    "id": "0x" + "a" * 64,
    "title": "Fund the thing",
    "body": "Please fund this.",
    "author": "0xauthor",
    "created": 1700000000,
    "end": 1700600000,
    "state": "closed",
    "link": "https://snapshot.org/#/arbitrumfoundation.eth/proposal/0x" + "a" * 64,
    "discussion": "https://forum.arbitrum.foundation/t/discuss/123",
    "choices": ["For", "Against"],
    "scores": [1234.5, 10.25],
    "scores_total": 1244.75,
    "scores_state": "final",
    "votes": 42,
    "space": {"id": "arbitrumfoundation.eth"},
}


def _forum(**overrides) -> kb.Forum:
    fields = dict(
        id=7,
        forum_slug="snap-arbitrum",
        base_url="https://hub.snapshot.org",
        kind="snapshot",
        category_ids=None,
        backfill_done=False,
        backfill_cursor={"space": "arbitrumfoundation.eth"},
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
    """Черга GraphQL-відповідей — по одній на кожен post_json()."""

    def __init__(self, pages: list[dict]):
        self.pages = list(pages)
        self.calls: list[tuple] = []

    def post_json(self, url, payload, headers=None):
        self.calls.append((url, payload, headers))
        if not self.pages:
            return _FakeResponse({"data": {"proposals": []}})
        return _FakeResponse(self.pages.pop(0))


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, newest_created_at=None):
        self.calls: list[tuple] = []
        self._next_id = 100
        self._newest_created_at = newest_created_at

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "RETURNING id" in sql:
            row = {"id": self._next_id}
            self._next_id += 1
            return _FakeCursor(row)
        if "max(created_at)" in sql:
            return _FakeCursor({"newest": self._newest_created_at})
        return _FakeCursor(None)

    def commit(self):
        pass


# ── _footer ─────────────────────────────────────────────────────────


def test_footer_renders_votes_result_state_and_discussion():
    footer = kb_snapshot._footer(PROPOSAL)
    assert footer.startswith("— Votes: 42 · Result: For: 1234.50, Against: 10.25 (final)")
    assert "Discussion: https://forum.arbitrum.foundation/t/discuss/123" in footer


def test_footer_omits_discussion_line_when_absent():
    proposal = dict(PROPOSAL, discussion="")
    footer = kb_snapshot._footer(proposal)
    assert "Discussion:" not in footer


def test_footer_handles_missing_choices_and_scores():
    footer = kb_snapshot._footer({"votes": 0, "scores_state": "pending"})
    assert footer == "— Votes: 0 · Result: n/a (pending)"


# ── _proposal_url ───────────────────────────────────────────────────


def test_proposal_url_prefers_link():
    url = kb_snapshot._proposal_url(PROPOSAL, "arbitrumfoundation.eth")
    assert url == PROPOSAL["link"]


def test_proposal_url_falls_back_when_no_link():
    proposal = dict(PROPOSAL, link=None)
    url = kb_snapshot._proposal_url(proposal, "arbitrumfoundation.eth")
    assert url == f"https://snapshot.org/#/arbitrumfoundation.eth/proposal/{PROPOSAL['id']}"


# ── _epoch_to_dt ────────────────────────────────────────────────────


def test_epoch_to_dt_converts_to_aware_utc():
    dt = kb_snapshot._epoch_to_dt(1700000000)
    assert dt.tzinfo is not None
    assert dt.timestamp() == 1700000000


def test_epoch_to_dt_none_passthrough():
    assert kb_snapshot._epoch_to_dt(None) is None


# ── _upsert_proposal: mapping shape + idempotent-upsert SQL ──────────


def test_upsert_proposal_stores_text_topic_id_with_footer_and_upsert_clauses():
    conn = _FakeConn()
    forum = _forum()

    kb_snapshot._upsert_proposal(conn, forum, PROPOSAL)

    assert len(conn.calls) == 2
    topic_sql, topic_params = conn.calls[0]
    assert "ON CONFLICT (forum_slug, topic_id) DO UPDATE" in topic_sql
    assert topic_params[0] == "snap-arbitrum"
    assert topic_params[1] == PROPOSAL["id"]
    assert isinstance(topic_params[1], str)  # topic_id — text (migration 008)
    assert topic_params[2] == "closed"  # category_name = state

    post_sql, post_params = conn.calls[1]
    assert "ON CONFLICT (topic_ref, post_number) DO UPDATE" in post_sql
    raw_text = post_params[-1]
    assert "Please fund this." in raw_text
    assert "Votes: 42" in raw_text
    assert "Discussion:" in raw_text


def test_upsert_proposal_url_falls_back_to_computed_link_when_missing():
    conn = _FakeConn()
    forum = _forum()
    proposal = dict(PROPOSAL, link=None)

    kb_snapshot._upsert_proposal(conn, forum, proposal)

    _, topic_params = conn.calls[0]
    url = topic_params[4]
    assert url == f"https://snapshot.org/#/arbitrumfoundation.eth/proposal/{PROPOSAL['id']}"


# ── _run_query: GraphQL error handling ────────────────────────────────


def test_run_query_raises_fetch_error_on_graphql_errors():
    client = _FakeHttpClient([{"errors": [{"message": "boom"}]}])
    with pytest.raises(FetchError):
        kb_snapshot._run_query(
            client, kb_snapshot.QUERY_PAGE, {"space": ["x"], "first": 1, "skip": 0}
        )


# ── backfill: paging + cursor advance/resume ──────────────────────────


def test_backfill_pages_until_a_short_page_and_saves_final_cursor():
    forum = _forum()
    conn = _FakeConn()
    full_page = {
        "data": {
            "proposals": [
                dict(PROPOSAL, id=f"0x{i:064x}") for i in range(kb_snapshot.PAGE_SIZE)
            ]
        }
    }
    short_page = {"data": {"proposals": [dict(PROPOSAL, id="0x" + "b" * 64)]}}
    client = _FakeHttpClient([full_page, short_page])

    stats = kb_snapshot.backfill(conn, client, forum)

    assert stats["topics_crawled"] == kb_snapshot.PAGE_SIZE + 1
    assert stats["pages_listed"] == 2

    _, second_payload, _ = client.calls[1]
    assert second_payload["variables"]["skip"] == kb_snapshot.PAGE_SIZE

    last_sql, last_params = conn.calls[-1]
    assert "backfill_done = true" in last_sql
    # Курсор по завершенні тримає ЛИШЕ space — 'skip' більше не потрібен,
    # а space нікуди більше не записаний і його читає incremental().
    assert json.loads(last_params[0]) == {"space": "arbitrumfoundation.eth"}


def test_backfill_resumes_from_the_saved_skip():
    forum = _forum(backfill_cursor={"space": "arbitrumfoundation.eth", "skip": 300})
    conn = _FakeConn()
    client = _FakeHttpClient([{"data": {"proposals": []}}])

    kb_snapshot.backfill(conn, client, forum)

    _, payload, _ = client.calls[0]
    assert payload["variables"]["skip"] == 300


def test_backfill_missing_space_raises_value_error():
    forum = _forum(backfill_cursor={})
    conn = _FakeConn()
    client = _FakeHttpClient([])

    with pytest.raises(ValueError):
        kb_snapshot.backfill(conn, client, forum)


def test_backfill_honors_max_topics_smoke_cap():
    forum = _forum()
    conn = _FakeConn()
    page = {
        "data": {"proposals": [dict(PROPOSAL, id=f"0x{i:064x}") for i in range(10)]}
    }
    client = _FakeHttpClient([page])

    stats = kb_snapshot.backfill(conn, client, forum, max_topics=3)

    assert stats["topics_crawled"] == 3
    assert not any("backfill_done = true" in sql for sql, _ in conn.calls)


def test_backfill_stops_on_should_stop_and_does_not_mark_done():
    forum = _forum()
    conn = _FakeConn()
    page = {
        "data": {"proposals": [dict(PROPOSAL, id=f"0x{i:064x}") for i in range(5)]}
    }
    client = _FakeHttpClient([page])
    seen = {"n": 0}

    def should_stop():
        seen["n"] += 1
        return seen["n"] > 2

    stats = kb_snapshot.backfill(conn, client, forum, should_stop=should_stop)

    assert 0 < stats["topics_crawled"] < 5
    assert not any("backfill_done = true" in sql for sql, _ in conn.calls)


# ── incremental: watermark read + advance ──────────────────────────────


def test_incremental_uses_last_post_seen_at_as_the_created_gt_watermark():
    watermark = datetime(2026, 1, 1, tzinfo=timezone.utc)
    forum = _forum(backfill_done=True, last_post_seen_at=watermark)
    conn = _FakeConn()
    newer = dict(PROPOSAL, id="0x" + "c" * 64, created=1800000000)
    client = _FakeHttpClient([{"data": {"proposals": [newer]}}])

    stats = kb_snapshot.incremental(conn, client, forum)

    assert stats["topics_crawled"] == 1
    _, payload, _ = client.calls[0]
    assert payload["variables"]["createdGt"] == int(watermark.timestamp())

    last_sql, last_params = conn.calls[-1]
    assert "last_post_seen_at = COALESCE" in last_sql
    assert last_params[0] == kb_snapshot._epoch_to_dt(1800000000)


def test_incremental_falls_back_to_max_created_at_when_no_watermark_yet():
    forum = _forum(backfill_done=True, last_post_seen_at=None)
    fallback = datetime(2025, 6, 1, tzinfo=timezone.utc)
    conn = _FakeConn(newest_created_at=fallback)
    client = _FakeHttpClient([{"data": {"proposals": []}}])

    kb_snapshot.incremental(conn, client, forum)

    _, payload, _ = client.calls[0]
    assert payload["variables"]["createdGt"] == int(fallback.timestamp())


def test_incremental_missing_space_raises_value_error():
    forum = _forum(backfill_cursor={}, backfill_done=True)
    conn = _FakeConn()
    client = _FakeHttpClient([])

    with pytest.raises(ValueError):
        kb_snapshot.incremental(conn, client, forum)


def test_incremental_paginates_when_more_than_one_page_is_new():
    watermark = datetime(2026, 1, 1, tzinfo=timezone.utc)
    forum = _forum(backfill_done=True, last_post_seen_at=watermark)
    conn = _FakeConn()
    full_page = {
        "data": {
            "proposals": [
                dict(PROPOSAL, id=f"0x{i:064x}", created=1800000000 + i)
                for i in range(kb_snapshot.INCREMENTAL_PAGE_SIZE)
            ]
        }
    }
    short_page = {"data": {"proposals": [dict(PROPOSAL, id="0x" + "d" * 64, created=1900000000)]}}
    client = _FakeHttpClient([full_page, short_page])

    stats = kb_snapshot.incremental(conn, client, forum)

    assert stats["topics_crawled"] == kb_snapshot.INCREMENTAL_PAGE_SIZE + 1
    assert len(client.calls) == 2
    _, second_payload, _ = client.calls[1]
    assert second_payload["variables"]["skip"] == kb_snapshot.INCREMENTAL_PAGE_SIZE
