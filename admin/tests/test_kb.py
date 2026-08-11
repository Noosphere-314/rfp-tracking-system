"""Тести /kb — покриття архівів (задача «покриття архівів», аудит
2026-08-11): kb.forums.remote_topics/remote_posts/stats_at (міграція 012)
рендеряться як видимий Coverage % поруч із «наші / на сайті» для Topics і
Posts. Без БД: `admin.app.db` підміняється фейковим conn.

kb_page робить ТРИ окремі запити за один GET (forums → results → query_log),
тож фейковий conn тут — черга наборів рядків, а не один фіксований `rows`
(як в інших тестах admin/tests/): попадання результатів пошуку в таблицю
форумів (чи навпаки) інакше пройшло б непоміченим.

Імпорт test_auth ПЕРШИМ — env виставляється до першого імпорту admin.app
(той самий трюк, що й в інших тестах admin/tests/)."""

from __future__ import annotations

from admin.tests.test_auth import _login, client  # noqa: E402,F401

from admin import app as admin_app  # noqa: E402


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Conn:
    """Черга наборів рядків — по одному на кожен execute()."""

    def __init__(self, rows_queue):
        self.rows_queue = list(rows_queue)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        rows = self.rows_queue.pop(0) if self.rows_queue else []
        return _Cursor(rows)

    def commit(self):
        pass


def _fake_db(monkeypatch, rows_queue):
    monkeypatch.setattr(admin_app, "db", lambda: _Conn(rows_queue))


def _forum(**over) -> dict:
    base = {
        "id": 1,
        "forum_slug": "optimism",
        "base_url": "https://gov.optimism.io",
        "enabled": True,
        "backfill_done": True,
        "consecutive_failures": 0,
        "topics": 80,
        "posts": 440,
        "newest_activity": None,
        "stale_days": 2,
        "last_incremental_at": None,
        "remote_topics": None,
        "remote_posts": None,
        "stats_at": None,
    }
    base.update(over)
    return base


def test_kb_page_shows_bad_coverage_badge_under_60_percent(client, monkeypatch):
    _login(client)
    _fake_db(
        monkeypatch,
        rows_queue=[
            [_forum(posts=440, remote_posts=1000, topics=80, remote_topics=120)],
            [],
            [],
        ],
    )
    response = client.get("/kb")
    assert response.status_code == 200
    html = response.text
    assert "44%" in html
    assert "b-bad" in html
    # «наші / на сайті»: 440 лишається як є (<1000), 1000 компактиться в 1k.
    assert "440" in html and "1k" in html


def test_kb_page_shows_warn_coverage_badge_in_60_89_band(client, monkeypatch):
    _login(client)
    _fake_db(
        monkeypatch,
        rows_queue=[[_forum(posts=700, remote_posts=1000)], [], []],
    )
    response = client.get("/kb")
    html = response.text
    assert "70%" in html
    assert "b-warn" in html


def test_kb_page_shows_ok_coverage_badge_at_90_percent_and_above(client, monkeypatch):
    _login(client)
    _fake_db(
        monkeypatch,
        rows_queue=[[_forum(posts=950, remote_posts=1000)], [], []],
    )
    response = client.get("/kb")
    html = response.text
    assert "95%" in html
    assert "b-ok" in html


def test_kb_page_shows_dash_and_neutral_badge_when_remote_counts_are_null(client, monkeypatch):
    """NULL remote_* — форум ще не мав жодного проходу воркера з еталоном
    /about.json: бейдж нейтральний, а не «0%», і підказує, що дані з'являться
    після наступного обходу."""
    _login(client)
    _fake_db(monkeypatch, rows_queue=[[_forum()], [], []])
    response = client.get("/kb")
    html = response.text
    assert "b-neutral" in html
    assert "reference numbers appear after the next crawl" in html


def test_kb_page_does_not_crash_when_remote_posts_is_zero(client, monkeypatch):
    """remote_posts = 0 (а не NULL) — еталон каже «на форумі 0 постів»:
    ділення на нуль тут не мусить валити сторінку, і трактується так само,
    як відсутній еталон (нейтральний бейдж, не ZeroDivisionError)."""
    _login(client)
    _fake_db(
        monkeypatch,
        rows_queue=[[_forum(posts=0, remote_posts=0, topics=0, remote_topics=0)], [], []],
    )
    response = client.get("/kb")
    assert response.status_code == 200
    assert "b-neutral" in response.text
