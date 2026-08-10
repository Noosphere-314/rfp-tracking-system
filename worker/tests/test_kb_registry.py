"""Тести worker/main.py: kb.forums.kind → crawler-модуль (KB_CRAWLERS реєстр,
worker.main._kb_crawlers(), міграція 008 — колонка kb.forums.kind).

Без БД і без мережі: kb.load_forums і crawler.backfill/incremental підмінені
monkeypatch'ем прямо на модулях worker.kb / worker.kb_snapshot (саме на них,
а не на _kb_crawlers(): _kb_crawlers() повертає посилання на самі модулі,
тож патч атрибута модуля видно і крізь реєстр — той самий трюк, що й
admin/tests з fake db()).

worker.main.connect() теж підмінений — HttpClient(conn) усередині
cmd_kb_backfill/cmd_kb_update конструюється по-справжньому (це безпечно:
конструктор нічого не читає з conn і не лізе в мережу, поки хтось явно не
викличе .get()/.post_json(), а мок-crawler'и цього не роблять)."""

from __future__ import annotations

import argparse
from contextlib import contextmanager

import pytest

from worker import kb, kb_snapshot, main


class _FakeConn:
    def execute(self, *a, **k):  # pragma: no cover — не мало б викликатись
        raise AssertionError(f"unexpected direct SQL in a dispatch test: {a!r}")

    def commit(self):
        pass

    def rollback(self):
        pass


@contextmanager
def _fake_connect():
    yield _FakeConn()


def _forum(slug: str, kind: str, backfill_done: bool = False) -> kb.Forum:
    return kb.Forum(
        id=1,
        forum_slug=slug,
        base_url="https://example.test",
        kind=kind,
        category_ids=None,
        backfill_done=backfill_done,
        backfill_cursor={},
        last_post_seen_at=None,
    )


def _spy(name: str, calls: list):
    def fn(conn, client, forum, **kwargs):
        calls.append((name, forum.forum_slug, kwargs))
        return {"topics_crawled": 0}

    return fn


@pytest.fixture
def calls():
    return []


# ── cmd_kb_backfill ─────────────────────────────────────────────────


def test_backfill_routes_discourse_and_snapshot_by_kind(monkeypatch, calls):
    forums = [_forum("optimism", "discourse"), _forum("snap-arbitrum", "snapshot")]
    monkeypatch.setattr(kb, "load_forums", lambda conn, only_slug=None: forums)
    monkeypatch.setattr(kb, "backfill", _spy("discourse", calls))
    monkeypatch.setattr(kb_snapshot, "backfill", _spy("snapshot", calls))
    monkeypatch.setattr(main, "connect", _fake_connect)

    rc = main.cmd_kb_backfill(argparse.Namespace(forum=None, max_topics=None, again=False))

    assert rc == 0
    routed = [(name, slug) for name, slug, _ in calls]
    assert routed == [("discourse", "optimism"), ("snapshot", "snap-arbitrum")]


def test_backfill_skips_unregistered_kind_without_raising(monkeypatch, calls):
    # 'site' — зарезервований kind (woof), навмисно без crawler-модуля.
    forums = [_forum("woof", "site"), _forum("optimism", "discourse")]
    monkeypatch.setattr(kb, "load_forums", lambda conn, only_slug=None: forums)
    monkeypatch.setattr(kb, "backfill", _spy("discourse", calls))
    monkeypatch.setattr(kb_snapshot, "backfill", _spy("snapshot", calls))
    monkeypatch.setattr(main, "connect", _fake_connect)

    rc = main.cmd_kb_backfill(argparse.Namespace(forum=None, max_topics=None, again=False))

    assert rc == 0
    assert [slug for _, slug, _ in calls] == ["optimism"]


def test_backfill_skips_future_unknown_kind_too(monkeypatch, calls):
    # kind з CHECK-обмеження (migration 008), для якого ще нема модуля.
    forums = [_forum("gh-something", "github")]
    monkeypatch.setattr(kb, "load_forums", lambda conn, only_slug=None: forums)
    monkeypatch.setattr(main, "connect", _fake_connect)

    rc = main.cmd_kb_backfill(argparse.Namespace(forum=None, max_topics=None, again=False))

    assert rc == 0
    assert calls == []


def test_backfill_passes_max_topics_and_should_stop_through(monkeypatch, calls):
    forums = [_forum("optimism", "discourse")]
    monkeypatch.setattr(kb, "load_forums", lambda conn, only_slug=None: forums)
    monkeypatch.setattr(kb, "backfill", _spy("discourse", calls))
    monkeypatch.setattr(main, "connect", _fake_connect)

    main.cmd_kb_backfill(argparse.Namespace(forum=None, max_topics=5, again=False))

    _, _, kwargs = calls[0]
    assert kwargs["max_topics"] == 5
    assert callable(kwargs["should_stop"])


def test_backfill_done_gate_still_applies_regardless_of_kind(monkeypatch, calls):
    forums = [_forum("snap-arbitrum", "snapshot", backfill_done=True)]
    monkeypatch.setattr(kb, "load_forums", lambda conn, only_slug=None: forums)
    monkeypatch.setattr(kb_snapshot, "backfill", _spy("snapshot", calls))
    monkeypatch.setattr(main, "connect", _fake_connect)

    rc = main.cmd_kb_backfill(argparse.Namespace(forum=None, max_topics=None, again=False))

    assert rc == 0
    assert calls == []  # backfill_done + no --again → gated, never reaches the crawler


def test_backfill_again_flag_bypasses_the_done_gate(monkeypatch, calls):
    forums = [_forum("snap-arbitrum", "snapshot", backfill_done=True)]
    monkeypatch.setattr(kb, "load_forums", lambda conn, only_slug=None: forums)
    monkeypatch.setattr(kb_snapshot, "backfill", _spy("snapshot", calls))
    monkeypatch.setattr(main, "connect", _fake_connect)

    main.cmd_kb_backfill(argparse.Namespace(forum=None, max_topics=None, again=True))

    assert [slug for _, slug, _ in calls] == ["snap-arbitrum"]


# ── cmd_kb_update ───────────────────────────────────────────────────


def test_update_routes_by_kind_and_gates_on_backfill_done(monkeypatch, calls):
    forums = [
        _forum("optimism", "discourse", backfill_done=True),
        _forum("snap-arbitrum", "snapshot", backfill_done=True),
        _forum("snap-lido", "snapshot", backfill_done=False),
    ]
    monkeypatch.setattr(kb, "load_forums", lambda conn, only_slug=None: forums)
    monkeypatch.setattr(kb, "incremental", _spy("discourse", calls))
    monkeypatch.setattr(kb_snapshot, "incremental", _spy("snapshot", calls))
    monkeypatch.setattr(main, "connect", _fake_connect)

    rc = main.cmd_kb_update(argparse.Namespace(forum=None))

    assert rc == 0
    assert [slug for _, slug, _ in calls] == ["optimism", "snap-arbitrum"]


def test_update_skips_unregistered_kind(monkeypatch, calls):
    forums = [_forum("woof", "site", backfill_done=True)]
    monkeypatch.setattr(kb, "load_forums", lambda conn, only_slug=None: forums)
    monkeypatch.setattr(main, "connect", _fake_connect)

    rc = main.cmd_kb_update(argparse.Namespace(forum=None))

    assert rc == 0
    assert calls == []
