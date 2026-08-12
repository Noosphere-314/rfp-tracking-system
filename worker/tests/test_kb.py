"""Тести worker/kb.py (kind='discourse'): crawl_topic() completeness-сигнал,
_categories() рекурсія по підкатегоріях + trip-wire попередження, _known_topics()
"скіп чи ні" з урахуванням повноти (не лише bumped_at), find_incomplete_topics()
+ repair() — катап-ап прохід по вже архівованих, але недобраних темах, і
_update_remote_stats() (/about.json → kb.forums.remote_*).

Контекст (DEVLOG 2026-08-11): архіви виявились суттєво неповними (пости
14-55% від еталону). Корінь — crawl_topic()'s tail-chunk цикл (добирає хвіст
довгого треду пачками по 50 post_ids) переривається на першому FetchError і
БІЛЬШЕ НЕ повертається до цієї теми: _upsert_topic вже встиг записати
СПРАВЖНІЙ bumped_at до того, як цикл почав добирати хвіст, тож наступний
прохід бачить "bumped_at не змінився" і скіпає тему назавжди, лишаючи її
частковою. Ці тести перевіряють обидві половини фіксу: наперед (crawl_topic
більше не бреше "все ок" через .complete; backfill/incremental перестають
скіпати неповну тему) і заднім числом (repair() добирає вже застряглі теми).

Без БД і без мережі: HttpClient підмінений чергою відповідей на .get() (той
самий стиль черги, що й .post_json() у test_kb_snapshot.py/test_kb_github.py,
тільки під інший метод), psycopg-з'єднання — фейком, що записує (sql, params)
і роздає канонічні рядки за розпізнаваним фрагментом SQL (RETURNING id /
HAVING / GROUP BY t.id) — той самий "fakes+monkeypatch" стиль, що й в
admin/tests та решті worker/tests."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from worker import kb
from worker.http import FetchError, SourceBlocked

_BUMPED = datetime(2026, 1, 2, tzinfo=timezone.utc)  # psycopg повертає datetime, не str


# ── fixtures & fakes ────────────────────────────────────────────────


def _forum(**overrides) -> kb.Forum:
    fields = dict(
        id=1,
        forum_slug="optimism",
        base_url="https://gov.optimism.io",
        kind="discourse",
        category_ids=None,
        backfill_done=False,
        backfill_cursor={},
        last_post_seen_at=None,
    )
    fields.update(overrides)
    return kb.Forum(**fields)


class _FakeResponse:
    def __init__(self, payload: dict, not_modified: bool = False):
        self._payload = payload
        self.not_modified = not_modified

    def json(self):
        return self._payload


class _FakeHttpClient:
    """Черга відповідей на .get() — по одній на кожен виклик, у тому порядку,
    у якому kb.py їх насправді робить (about.json, categories.json, лістинг
    сторінками, потім деталі теми + хвіст-чанки)."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[tuple] = []

    def get(self, url, headers=None, use_cache=True):
        self.calls.append((url, use_cache))
        if not self.responses:
            raise AssertionError(f"no more fake responses queued; got GET {url}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeCursor:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows

    def __iter__(self):
        # _known_topics() iterates `conn.execute(...)` directly (a real
        # psycopg cursor is iterable) rather than calling .fetchall().
        return iter(self._rows)


class _FakeConn:
    """known_rows живить _known_topics() (GROUP BY t.id, без HAVING),
    incomplete_rows живить find_incomplete_topics() (той самий джойн, але з
    HAVING count(p.id) < t.post_count - tolerance) — розрізняємо запити за
    наявністю "HAVING" у SQL, а не за позицією виклику."""

    def __init__(self, known_rows=None, incomplete_rows=None):
        self.calls: list[tuple] = []
        self._next_id = 900
        self.known_rows = known_rows or []
        self.incomplete_rows = incomplete_rows or []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "RETURNING id" in sql:
            row = {"id": self._next_id}
            self._next_id += 1
            return _FakeCursor(row=row)
        if "HAVING count(p.id) < t.post_count" in sql:
            return _FakeCursor(rows=self.incomplete_rows)
        if "GROUP BY t.id" in sql:
            return _FakeCursor(rows=self.known_rows)
        return _FakeCursor()

    def commit(self):
        pass

    def rollback(self):
        pass


def _post(post_id: int, post_number: int, text: str = "hello") -> dict:
    return {
        "id": post_id,
        "post_number": post_number,
        "username": "alice",
        "created_at": "2026-01-01T00:00:00Z",
        "cooked": f"<p>{text}</p>",
    }


def _topic_payload(topic_id: int, stream: list[int], included: list[dict], **extra) -> dict:
    payload = {
        "id": topic_id,
        "title": f"Topic {topic_id}",
        "slug": "thread",
        "category_id": 10,
        "created_at": "2026-01-01T00:00:00Z",
        "last_posted_at": "2026-01-02T00:00:00Z",
        "posts_count": len(stream),
        "post_stream": {"stream": stream, "posts": included},
    }
    payload.update(extra)
    return payload


# ── crawl_topic(): .complete signal ──────────────────────────────────


def test_crawl_topic_reports_complete_when_every_chunk_fetches_ok():
    forum = _forum()
    conn = _FakeConn()
    topic = _topic_payload(555, stream=[1, 2, 3], included=[_post(1, 1), _post(2, 2)])
    chunk = {"post_stream": {"posts": [_post(3, 3)]}}
    client = _FakeHttpClient([_FakeResponse(topic), _FakeResponse(chunk)])

    result = kb.crawl_topic(conn, client, forum, 555)

    assert result.complete is True
    assert result.stored == 3  # 2 included + 1 from the chunk


def test_crawl_topic_reports_incomplete_when_a_tail_chunk_fails():
    """Це і є корінь бага: FetchError на одному чанку не мусить виглядати як
    "тему повністю забрано" для викликача — інакше backfill/incremental
    скіпатимуть її назавжди (див. _known_topics)."""
    forum = _forum()
    conn = _FakeConn()
    topic = _topic_payload(555, stream=[1, 2, 3], included=[_post(1, 1)])
    client = _FakeHttpClient([_FakeResponse(topic), FetchError("boom")])

    result = kb.crawl_topic(conn, client, forum, 555)

    assert result.complete is False
    assert result.stored == 1  # лише included — чанк так і не додався


def test_crawl_topic_not_modified_is_complete_with_zero_stored():
    forum = _forum()
    conn = _FakeConn()
    client = _FakeHttpClient([_FakeResponse({}, not_modified=True)])

    result = kb.crawl_topic(conn, client, forum, 555)

    assert result == kb.CrawlResult(stored=0, complete=True)


# ── _categories(): subcategory walk + whitelist + trip-wire ──────────


def _category(id_, slug, subcats=None, sub_ids=None, name=None):
    cat = {"id": id_, "slug": slug, "name": name or slug}
    if subcats is not None:
        cat["subcategory_list"] = subcats
    if sub_ids is not None:
        cat["subcategory_ids"] = sub_ids
    return cat


def test_categories_walks_two_level_nesting():
    # Форма живої відповіді discuss.ens.domains (перевірено наживо 2026-08-11):
    # include_subcategories=true вкладає повний subcategory_list під кожну
    # top-level категорію.
    payload = {
        "category_list": {
            "categories": [
                _category(
                    48, "dao-wide", sub_ids=[49, 50],
                    subcats=[_category(49, "general-discussion"), _category(50, "temp-check")],
                ),
                _category(24, "general-discussion-top"),
            ]
        }
    }
    client = _FakeHttpClient([_FakeResponse(payload)])
    forum = _forum()

    flat = kb._categories(client, forum)

    assert {c["id"] for c in flat} == {48, 49, 50, 24}


def test_categories_applies_whitelist_when_forum_has_category_ids():
    payload = {
        "category_list": {
            "categories": [_category(1, "a"), _category(2, "b"), _category(3, "c")]
        }
    }
    client = _FakeHttpClient([_FakeResponse(payload)])
    forum = _forum(category_ids=[1, 3])

    flat = kb._categories(client, forum)

    assert {c["id"] for c in flat} == {1, 3}


def test_categories_warns_when_subcategory_ids_outnumber_embedded_list(caplog):
    """Trip-wire для гіпотези "categories.json не віддає онуків": якщо колись
    subcategory_ids довший за subcategory_list, це мало б бути видно в логах,
    а не тихо загубленими темами."""
    payload = {
        "category_list": {
            "categories": [
                _category(1, "parent", sub_ids=[2, 3], subcats=[_category(2, "child")]),
            ]
        }
    }
    client = _FakeHttpClient([_FakeResponse(payload)])
    forum = _forum()

    with caplog.at_level(logging.WARNING):
        flat = kb._categories(client, forum)

    assert {c["id"] for c in flat} == {1, 2}  # category 3 навіть не з'явилась
    assert any("subcategory id" in r.message for r in caplog.records)


# ── _known_topics(): completeness drives the skip decision ───────────


def test_known_topics_marks_complete_when_have_meets_post_count():
    conn = _FakeConn(known_rows=[
        {"topic_id": "1", "bumped_at": "2026-01-01", "post_count": 10, "have": 10},
        {"topic_id": "2", "bumped_at": "2026-01-01", "post_count": 10, "have": 9},  # within tolerance
    ])
    known = kb._known_topics(conn, _forum())

    assert known["1"].complete is True
    assert known["2"].complete is True  # 10 - 9 == 1 <= REPAIR_TOLERANCE(2)


def test_known_topics_marks_incomplete_beyond_tolerance():
    conn = _FakeConn(known_rows=[
        {"topic_id": "3", "bumped_at": "2026-01-01", "post_count": 10, "have": 4},
    ])
    known = kb._known_topics(conn, _forum())

    assert known["3"].complete is False


def test_known_topics_treats_null_post_count_as_complete():
    """post_count відсутній (старий рядок / джерело без цього поля) — нема з
    чим звірятись, тож не варто перекроулювати таку тему щоразу."""
    conn = _FakeConn(known_rows=[
        {"topic_id": "4", "bumped_at": "2026-01-01", "post_count": None, "have": 0},
    ])
    known = kb._known_topics(conn, _forum())

    assert known["4"].complete is True


# ── backfill(): a partially-saved topic is NOT skipped forever ───────


def test_backfill_recrawls_a_known_but_incomplete_topic_despite_unchanged_bumped_at():
    """Це — головна регресія з діагнозу: тема, вже відома з тим самим
    bumped_at, що й лістинг щойно повернув, АЛЕ з локально недобраними
    постами, мусить бути перекроульована, а не позначена "skipped"."""
    forum = _forum(backfill_cursor={})
    conn = _FakeConn(known_rows=[
        {"topic_id": "555", "bumped_at": _BUMPED, "post_count": 10, "have": 3},  # відомо, але недобрано
    ])
    about = {"about": {"stats": {"topics_count": 100, "posts_count": 1000}}}
    categories_payload = {"category_list": {"categories": [_category(10, "general")]}}
    listing_page0 = {
        "topic_list": {
            "topics": [{"id": 555, "bumped_at": "2026-01-02T00:00:00.000Z", "slug": "thread"}]
        }
    }
    listing_page1 = {"topic_list": {"topics": []}}
    topic_detail = _topic_payload(555, stream=[1, 2, 3], included=[_post(1, 1), _post(2, 2)])
    chunk = {"post_stream": {"posts": [_post(3, 3)]}}

    client = _FakeHttpClient([
        _FakeResponse(about),
        _FakeResponse(categories_payload),
        _FakeResponse(listing_page0),
        _FakeResponse(topic_detail),
        _FakeResponse(chunk),
        _FakeResponse(listing_page1),
    ])

    stats = kb.backfill(conn, client, forum)

    assert stats["skipped"] == 0
    assert stats["topics_crawled"] == 1
    assert stats["posts_stored"] == 3


def test_backfill_skips_a_known_complete_topic_with_unchanged_bumped_at():
    """Контроль-регрес: звичайний "нічого не змінилось" скіп мусить лишитись
    робочим, інакше кожен прогін перекроулював би геть усе заново."""
    forum = _forum(backfill_cursor={})
    conn = _FakeConn(known_rows=[
        {"topic_id": "555", "bumped_at": _BUMPED, "post_count": 3, "have": 3},  # повністю забрано
    ])
    about = {"about": {"stats": {"topics_count": 100, "posts_count": 1000}}}
    categories_payload = {"category_list": {"categories": [_category(10, "general")]}}
    listing_page0 = {
        "topic_list": {
            "topics": [{"id": 555, "bumped_at": "2026-01-02T00:00:00.000Z", "slug": "thread"}]
        }
    }
    listing_page1 = {"topic_list": {"topics": []}}

    client = _FakeHttpClient([
        _FakeResponse(about),
        _FakeResponse(categories_payload),
        _FakeResponse(listing_page0),
        _FakeResponse(listing_page1),
    ])

    stats = kb.backfill(conn, client, forum)

    assert stats["skipped"] == 1
    assert stats["topics_crawled"] == 0
    # ніякого GET на /t/555.json — черга дійшла до кінця без AssertionError


# ── find_incomplete_topics() / repair() ───────────────────────────────


def test_find_incomplete_topics_returns_only_undershooting_topics():
    conn = _FakeConn(incomplete_rows=[{"topic_id": "42"}, {"topic_id": "43"}])

    ids = kb.find_incomplete_topics(conn, _forum())

    assert ids == ["42", "43"]
    sql, params = conn.calls[-1]
    assert "HAVING count(p.id) < t.post_count" in sql
    assert params == ("optimism", kb.REPAIR_TOLERANCE)


def test_repair_recrawls_every_candidate_topic():
    forum = _forum()
    conn = _FakeConn(incomplete_rows=[{"topic_id": "42"}])
    topic = _topic_payload(42, stream=[1, 2], included=[_post(1, 1), _post(2, 2)])
    client = _FakeHttpClient([_FakeResponse(topic)])

    stats = kb.repair(conn, client, forum)

    assert stats["candidates"] == 1
    assert stats["topics_crawled"] == 1
    assert stats["posts_stored"] == 2
    assert stats["still_incomplete"] == 0
    assert client.calls[0][0] == f"{forum.base_url}/t/42.json"


def test_repair_counts_topics_still_incomplete_after_a_retry():
    forum = _forum()
    conn = _FakeConn(incomplete_rows=[{"topic_id": "42"}])
    topic = _topic_payload(42, stream=[1, 2, 3], included=[_post(1, 1)])
    client = _FakeHttpClient([_FakeResponse(topic), FetchError("still failing")])

    stats = kb.repair(conn, client, forum)

    assert stats["topics_crawled"] == 1
    assert stats["still_incomplete"] == 1


def test_repair_honors_max_topics():
    forum = _forum()
    conn = _FakeConn(incomplete_rows=[{"topic_id": "1"}, {"topic_id": "2"}])
    topic1 = _topic_payload(1, stream=[10], included=[_post(10, 1)])
    client = _FakeHttpClient([_FakeResponse(topic1)])

    stats = kb.repair(conn, client, forum, max_topics=1)

    assert stats["topics_crawled"] == 1
    assert stats["candidates"] == 2


def test_repair_reraises_source_blocked_to_stop_the_forum():
    forum = _forum()
    conn = _FakeConn(incomplete_rows=[{"topic_id": "1"}])
    client = _FakeHttpClient([SourceBlocked("403")])

    with pytest.raises(SourceBlocked):
        kb.repair(conn, client, forum)


# ── _update_remote_stats(): /about.json → kb.forums.remote_* ─────────


def test_update_remote_stats_writes_topics_and_posts_from_about_json():
    forum = _forum()
    conn = _FakeConn()
    about = {"about": {"stats": {"topics_count": 2721, "posts_count": 59011}}}
    client = _FakeHttpClient([_FakeResponse(about)])

    kb._update_remote_stats(conn, client, forum)

    sql, params = conn.calls[-1]
    assert "UPDATE kb.forums SET remote_topics" in sql
    assert params == (2721, 59011, forum.id)


def test_update_remote_stats_is_best_effort_and_does_not_raise():
    forum = _forum()
    conn = _FakeConn()
    client = _FakeHttpClient([FetchError("network blip")])

    kb._update_remote_stats(conn, client, forum)  # не мало б кинути

    assert not any("UPDATE kb.forums SET remote_topics" in sql for sql, _ in conn.calls)


def test_backfill_updates_remote_stats_before_walking_categories():
    forum = _forum(backfill_cursor={})
    conn = _FakeConn()
    about = {"about": {"stats": {"topics_count": 5, "posts_count": 50}}}
    categories_payload = {"category_list": {"categories": []}}
    client = _FakeHttpClient([_FakeResponse(about), _FakeResponse(categories_payload)])

    kb.backfill(conn, client, forum)

    assert client.calls[0][0] == f"{forum.base_url}/about.json"
    assert any("remote_topics" in sql for sql, _ in conn.calls)


def test_crawl_topic_tolerates_null_topic_json():
    """Discourse може відповісти 200 з literal `null` (прихована/видалена
    тема): раніше .get() на None валив УВЕСЬ прогін форуму (Celo
    2026-08-11). Тепер — попередження і complete=True, щоб примара не
    поверталась у кандидати вічно."""
    forum = _forum()
    conn = _FakeConn()
    client = _FakeHttpClient([_FakeResponse(None)])

    result = kb.crawl_topic(conn, client, forum, 999)

    assert result.stored == 0
    assert result.complete is True


def test_crawl_topic_tolerates_null_created_by():
    """created_by: null (анонімізований/видалений автор) — справжній корінь
    падіння Celo 2026-08-12: .get("created_by", {}) повертає None, коли ключ
    ІСНУЄ зі значенням null (дефолт лише для відсутнього ключа)."""
    forum = _forum()
    conn = _FakeConn()
    topic = _topic_payload(777, stream=[1], included=[_post(1, 1)])
    topic["details"] = {"created_by": None}
    client = _FakeHttpClient([_FakeResponse(topic)])

    result = kb.crawl_topic(conn, client, forum, 777)

    assert result.complete is True
    assert result.stored == 1
