"""Тести SQL-ядра kbtools.py — без Postgres: kbtools._db підмінюється
fake-з'єднанням (fakedb.py), яке роутить запити за підрядком SQL. Ціль —
довести, що search_impl/get_topic_impl лишились байт-в-байт тими самими
tool-функціями, що жили в server.py до винесення (форма відповіді, hint-и,
виклики _log)."""

from __future__ import annotations

import os
import sys

# Env — ДО імпорту kbtools: DATABASE_URL читається на рівні модуля й падає
# з KeyError, якщо взагалі відсутній (той самий fail-fast стиль, що й у
# admin/auth.py — див. admin/tests/test_auth.py).
os.environ.setdefault("DATABASE_URL", "postgresql://x:x@127.0.0.1:1/x")

# Плоский імпорт, як у контейнері: mcp/Dockerfile копіює mcp/*.py в один
# /app, тож kbtools.py імпортує сусідні модулі без префікса пакета.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import kbtools  # noqa: E402
from fakedb import make_db  # noqa: E402


# ── search_impl ──────────────────────────────────────────────────────


def test_search_impl_shapes_post_and_topic_hits(monkeypatch):
    post_row = {
        "forum_slug": "optimism", "topic_id": 42, "title": "Grants RFP",
        "category_name": "grants",
        "post_url": "https://gov.optimism.io/t/x/42/3",
        "post_number": 3, "author": "alice", "posted_at": None,
        "snippet": "«RFP» snippet",
    }
    topic_row = {
        "forum_slug": "optimism", "topic_id": 43, "title": "Grants title-only",
        "category_name": "grants", "url": "https://gov.optimism.io/t/y/43",
        "bumped_at": None, "post_count": 5,
    }
    router = [
        ("ts_rank_cd(p.body_tsv", [post_row]),
        ("t.title_tsv @@ q", [topic_row]),
        ("INSERT INTO kb.query_log", []),
    ]
    db_factory, calls = make_db(router)
    monkeypatch.setattr(kbtools, "_db", db_factory)

    result = kbtools.search_impl("grants rfp", limit=10)

    assert result["post_hits"] == [{
        "forum": "optimism", "topic_id": 42, "title": "Grants RFP",
        "category": "grants", "post_url": post_row["post_url"],
        "post_number": 3, "author": "alice", "posted_at": None,
        "snippet": "«RFP» snippet",
    }]
    assert result["topic_title_hits"][0]["title"] == "Grants title-only"
    assert "Read full threads" in result["hint"]
    assert any("INSERT INTO kb.query_log" in sql for sql, _ in calls)
    # tool="search_kb" (не "search_impl") — chat.py й server.py повинні лишати
    # kb.query_log сумісним із тим, що там уже накопичено.
    log_call = next(p for sql, p in calls if "INSERT INTO kb.query_log" in sql)
    assert log_call[0] == "search_kb"
    assert log_call[3] == 1  # len(hits)


def test_search_impl_empty_hits_hint_suggests_rewording(monkeypatch):
    router = [
        ("ts_rank_cd(p.body_tsv", []),
        ("t.title_tsv @@ q", []),
        ("INSERT INTO kb.query_log", []),
    ]
    db_factory, _calls = make_db(router)
    monkeypatch.setattr(kbtools, "_db", db_factory)

    result = kbtools.search_impl("nonsense query")

    assert result["post_hits"] == []
    assert result["topic_title_hits"] == []
    assert "Reword" in result["hint"]


def test_search_impl_bad_after_date_short_circuits_before_any_query(monkeypatch):
    def must_not_be_called():
        raise AssertionError("a malformed `after` must not reach the database")

    monkeypatch.setattr(kbtools, "_db", must_not_be_called)

    result = kbtools.search_impl("x", after="not-a-date")

    assert "after" in result["error"]


def test_search_impl_limit_is_clamped_to_50(monkeypatch):
    captured = {}

    def capture_limit(params):
        captured["limit"] = params["limit"]
        return []

    router = [
        ("ts_rank_cd(p.body_tsv", capture_limit),
        ("t.title_tsv @@ q", []),
        ("INSERT INTO kb.query_log", []),
    ]
    db_factory, _calls = make_db(router)
    monkeypatch.setattr(kbtools, "_db", db_factory)

    kbtools.search_impl("x", limit=999)

    assert captured["limit"] == 50


# ── topic_impl ───────────────────────────────────────────────────────


def test_topic_impl_unknown_topic_returns_error_and_logs_zero_hits(monkeypatch):
    router = [
        ("FROM kb.topics WHERE forum_slug", []),  # fetchone() -> None
        ("INSERT INTO kb.query_log", []),
    ]
    db_factory, calls = make_db(router)
    monkeypatch.setattr(kbtools, "_db", db_factory)

    result = kbtools.topic_impl("optimism", 999)

    assert result == {"error": "topic 999 not in the optimism archive"}
    log_call = next(p for sql, p in calls if "INSERT INTO kb.query_log" in sql)
    assert log_call == ("get_topic", "999", "optimism", 0)


def test_topic_impl_returns_posts_with_citation_urls(monkeypatch):
    topic_row = {
        "id": 7, "title": "T", "url": "https://gov.optimism.io/t/x/7",
        "category_name": "grants", "created_at": None, "post_count": 2,
    }
    post_row = {
        "post_number": 1, "author": "bob", "posted_at": None, "raw_text": "hello",
    }
    router = [
        ("FROM kb.topics WHERE forum_slug", [topic_row]),
        ("FROM kb.posts WHERE topic_ref", [post_row]),
        ("INSERT INTO kb.query_log", []),
    ]
    db_factory, _calls = make_db(router)
    monkeypatch.setattr(kbtools, "_db", db_factory)

    result = kbtools.topic_impl("optimism", 7, max_posts=999)

    assert result["title"] == "T"
    assert result["returned"] == 1
    assert result["posts"][0]["cite"] == "https://gov.optimism.io/t/x/7/1"


def test_topic_impl_max_posts_is_clamped_to_200(monkeypatch):
    captured = {}

    def capture(params):
        captured["max_posts"] = params[2]
        return []

    router = [
        ("FROM kb.topics WHERE forum_slug", [{
            "id": 1, "title": "T", "url": "u", "category_name": None,
            "created_at": None, "post_count": 0,
        }]),
        ("FROM kb.posts WHERE topic_ref", capture),
        ("INSERT INTO kb.query_log", []),
    ]
    db_factory, _calls = make_db(router)
    monkeypatch.setattr(kbtools, "_db", db_factory)

    kbtools.topic_impl("optimism", 1, max_posts=10_000)

    assert captured["max_posts"] == 200
