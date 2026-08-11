"""Тести briefing.py — новий файл: до kbmcp agent 2.0 (011_brief_depth.sql,
"повзунок глибини") briefing.py взагалі не мав тестів. Стратегія та сама, що
й у test_chat.py/test_kbtools.py: без Postgres і без мережі, briefing._db
підмінюється fakedb.make_db, anthropic — fakeanthropic.install.

Фокус тут вузький — лише те, що змінилося заради C (per-call injection
brief_max_words у _SYSTEM): clamp, _llm_brief/make_brief wiring, і кілька
регресійних тестів на існуючу гілку basic/llm та "невідома екосистема", щоб
переконатися, що зміна сигнатури _llm_brief нічого не зламала мовчки (виняток
у make_brief ковтається try/except — тест з монкіпатченим _llm_brief перевіряє
саме wiring, а не залежить від того, що станеться всередині _llm_brief)."""

from __future__ import annotations

import os
import sys

# Env — ДО імпорту briefing: DATABASE_URL читається на рівні модуля й падає
# з KeyError, якщо взагалі відсутній (той самий стиль, що й kbtools.py).
os.environ.setdefault("DATABASE_URL", "postgresql://x:x@127.0.0.1:1/x")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import briefing  # noqa: E402
from fakeanthropic import FakeResponse, install as install_fake_anthropic, text_block  # noqa: E402
from fakedb import make_db  # noqa: E402


# ── C: brief_max_words clamp (briefing._brief_max_words) ────────────


def test_brief_max_words_clamps_below_floor():
    db_factory, _ = make_db([("SELECT value FROM settings", [{"value": "10"}])])
    assert briefing._brief_max_words(db_factory()) == 100


def test_brief_max_words_clamps_above_ceiling():
    db_factory, _ = make_db([("SELECT value FROM settings", [{"value": "9999"}])])
    assert briefing._brief_max_words(db_factory()) == 2000


def test_brief_max_words_non_numeric_falls_back_to_350():
    db_factory, _ = make_db([("SELECT value FROM settings", [{"value": "not-a-number"}])])
    assert briefing._brief_max_words(db_factory()) == 350


def test_brief_max_words_missing_setting_falls_back_to_350():
    db_factory, _ = make_db([])  # no row → _setting's own default kicks in
    assert briefing._brief_max_words(db_factory()) == 350


def test_brief_max_words_within_range_passes_through():
    db_factory, _ = make_db([("SELECT value FROM settings", [{"value": "700"}])])
    assert briefing._brief_max_words(db_factory()) == 700


# ── C: _SYSTEM template ──────────────────────────────────────────────


def test_system_template_formats_configured_word_count():
    text = briefing._SYSTEM.format(max_words=275)
    assert "under 275 words" in text
    assert "{max_words}" not in text  # no leftover placeholder


# ── make_brief: unknown ecosystem (pre-existing behaviour, unaffected) ──


def test_make_brief_unknown_ecosystem_returns_error(monkeypatch):
    router = [("ecosystem IS NOT NULL", [{"forum_slug": "arbitrum"}])]
    db_factory, _calls = make_db(router)
    monkeypatch.setattr(briefing, "_db", db_factory)

    result = briefing.make_brief("Nonexistent", "Some title")

    assert "error" in result
    assert result["archived_ecosystems"] == ["arbitrum"]


# ── make_brief: basic tier when no ANTHROPIC_API_KEY ─────────────────


def test_make_brief_basic_tier_when_no_api_key(monkeypatch):
    monkeypatch.setattr(briefing, "ANTHROPIC_API_KEY", "")
    router = [
        ("FROM kb.forums", [{"forum_slug": "optimism", "base_url": "https://gov.optimism.io"}]),
        ("INSERT INTO kb.briefs", [{"id": 2}]),
    ]
    db_factory, _calls = make_db(router)
    monkeypatch.setattr(briefing, "_db", db_factory)

    result = briefing.make_brief("Optimism", "Title", "body")

    assert result["tier"] == "basic"
    assert result["model"] is None
    assert "KB brief: Optimism" in result["brief_md"]


# ── make_brief → _llm_brief wiring: max_words threaded through ───────


def test_make_brief_passes_configured_max_words_to_llm_brief(monkeypatch):
    monkeypatch.setattr(briefing, "ANTHROPIC_API_KEY", "sk-test")

    def settings_response(params):
        key = params[0]
        return {
            "brief_model": [{"value": "claude-opus-5"}],
            "brief_language": [{"value": "en"}],
            "brief_max_words": [{"value": "700"}],
        }.get(key, [])

    router = [
        ("FROM kb.forums", [{"forum_slug": "optimism", "base_url": "https://gov.optimism.io"}]),
        ("SELECT value FROM settings", settings_response),
        ("INSERT INTO kb.briefs", [{"id": 9}]),
    ]
    db_factory, _calls = make_db(router)
    monkeypatch.setattr(briefing, "_db", db_factory)

    seen = {}

    def fake_llm_brief(conn, forum_slug, ecosystem, title, body, model, language, max_words):
        seen["max_words"] = max_words
        seen["model"] = model
        seen["language"] = language
        return "brief text", 1, 2

    monkeypatch.setattr(briefing, "_llm_brief", fake_llm_brief)

    result = briefing.make_brief("Optimism", "Title", "body")

    assert seen == {"max_words": 700, "model": "claude-opus-5", "language": "en"}
    assert result["tier"] == "llm"
    assert result["brief_id"] == 9


def test_make_brief_clamps_bad_max_words_setting_before_passing_it_on(monkeypatch):
    monkeypatch.setattr(briefing, "ANTHROPIC_API_KEY", "sk-test")

    def settings_response(params):
        key = params[0]
        return {
            "brief_model": [{"value": "claude-opus-5"}],
            "brief_language": [{"value": "en"}],
            "brief_max_words": [{"value": "garbage"}],  # → clamp default, not a 500
        }.get(key, [])

    router = [
        ("FROM kb.forums", [{"forum_slug": "optimism", "base_url": "https://gov.optimism.io"}]),
        ("SELECT value FROM settings", settings_response),
        ("INSERT INTO kb.briefs", [{"id": 1}]),
    ]
    db_factory, _calls = make_db(router)
    monkeypatch.setattr(briefing, "_db", db_factory)

    seen = {}

    def fake_llm_brief(conn, forum_slug, ecosystem, title, body, model, language, max_words):
        seen["max_words"] = max_words
        return "brief text", 1, 2

    monkeypatch.setattr(briefing, "_llm_brief", fake_llm_brief)

    briefing.make_brief("Optimism", "Title", "body")

    assert seen["max_words"] == 350


# ── _llm_brief: the configured word count actually reaches the prompt ──


def test_llm_brief_injects_configured_max_words_into_system_prompt(monkeypatch):
    monkeypatch.setattr(briefing, "ANTHROPIC_API_KEY", "sk-test")
    db_factory, _calls = make_db([])  # _gather_context: no matches → empty grounding
    conn = db_factory()

    responses = [FakeResponse([text_block("Brief body.")], "end_turn",
                               tokens_in=10, tokens_out=20)]
    holder = install_fake_anthropic(monkeypatch, responses)

    text, tokens_in, tokens_out = briefing._llm_brief(
        conn, "optimism", "Optimism", "Some RFP title", "body text",
        "claude-opus-5", "en", 120,
    )

    assert text == "Brief body."
    assert tokens_in == 10
    assert tokens_out == 20

    call = holder["client"].messages.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["max_tokens"] == 4000
    system_block = call["system"][0]
    assert "under 120 words" in system_block["text"]
    assert system_block["cache_control"] == {"type": "ephemeral"}
