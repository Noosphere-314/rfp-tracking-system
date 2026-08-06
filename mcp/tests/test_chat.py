"""Тести чат-агента chat.py — без Postgres і без мережі. Стратегія:

  - Валідація контракту й fail-closed перевіряються прямим викликом
    chat.answer(), бо вони спрацьовують ДО будь-якого звернення до БД.
  - Усе, що торкається БД (rate-limit, персист, історія), тестується через
    monkeypatch окремих функцій-помічників chat.py (_recent_user_count,
    _daily_budget_exceeded, _insert_message, _load_history) — раз SQL-шар
    kbtools вже перевірений окремо в test_kbtools.py, тут важлива лише
    ОРКЕСТРАЦІЯ (порядок викликів, форма відповіді), а не сам SQL.
  - LLM-цикл (_llm_reply) тестується з підміненим sys.modules['anthropic']
    (fakeanthropic.py) — форма об'єктів повторює briefing.py.
"""

from __future__ import annotations

import os
import sys

# Env — ДО імпорту chat/kbtools: обидва модулі читають DATABASE_URL на рівні
# модуля (падають з KeyError, якщо його взагалі нема). KB_MCP_TOKEN
# встановлюємо тут же, як «типове» значення для тестів — окремі тести
# fail-closed самі перезаписують його через monkeypatch.setenv/delenv.
os.environ.setdefault("DATABASE_URL", "postgresql://x:x@127.0.0.1:1/x")
os.environ["KB_MCP_TOKEN"] = "test-token"
os.environ.setdefault("ANTHROPIC_API_KEY", "")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402

import chat  # noqa: E402
import kbtools  # noqa: E402
from fakeanthropic import (  # noqa: E402
    FakeResponse, install as install_fake_anthropic, text_block, tool_use_block,
)

VALID_PAYLOAD = {
    "channel": "web",
    "session_key": "sess-1",
    "who": "mykola",
    "message": "What grants has Optimism funded for dev tooling?",
}


def _raise(*_a, **_kw):
    raise RuntimeError("boom")


def _stub_no_op_db(monkeypatch, *, budget_exceeded=False):
    """Прибирає chat.answer від БД для тестів, яким цікава лише оркестрація
    (не сам SQL — той перевірений у test_kbtools.py)."""
    monkeypatch.setattr(chat, "_recent_user_count", lambda storage_key: 0)
    monkeypatch.setattr(chat, "_daily_budget_exceeded", lambda: budget_exceeded)
    monkeypatch.setattr(chat, "_load_history", lambda storage_key, before_id: [])
    monkeypatch.setattr(
        chat, "_insert_message",
        lambda storage_key, channel, who, role, content, **kw: 1,
    )


# ── Fail-closed (KB_MCP_TOKEN unset) ────────────────────────────────


def test_fail_closed_when_token_unset(monkeypatch):
    monkeypatch.delenv("KB_MCP_TOKEN", raising=False)
    body, status = chat.answer(VALID_PAYLOAD)
    assert status == 403
    assert body == {
        "ok": False,
        "error": "Chat is disabled: KB_MCP_TOKEN is not set.",
    }


def test_fail_closed_when_token_empty_string(monkeypatch):
    monkeypatch.setenv("KB_MCP_TOKEN", "")
    body, status = chat.answer(VALID_PAYLOAD)
    assert status == 403


def test_fail_closed_takes_priority_over_contract_validation(monkeypatch):
    monkeypatch.setenv("KB_MCP_TOKEN", "")
    body, status = chat.answer({"garbage": True})
    assert status == 403  # not 400 — the gate runs before validation


# ── Contract validation (400s) ──────────────────────────────────────


@pytest.mark.parametrize(
    "overrides,expected_snippet",
    [
        ({"channel": "sms"}, "channel"),
        ({"channel": None}, "channel"),
        ({"session_key": ""}, "session_key is required"),
        ({"session_key": None}, "session_key is required"),
        ({"session_key": "a" * 161}, "at most 160"),
        ({"session_key": "web:room1"}, "must not contain"),
        ({"who": "x" * 65}, "who must be at most"),
        ({"who": 12345}, "who must be a string"),
        ({"message": ""}, "message is required"),
        ({"message": "   "}, "message is required"),
        ({"message": "x" * 4001}, "at most 4000"),
        ({"message": 123}, "message is required"),
    ],
)
def test_contract_validation_400(monkeypatch, overrides, expected_snippet):
    payload = {**VALID_PAYLOAD, **overrides}
    body, status = chat.answer(payload)
    assert status == 400
    assert body["ok"] is False
    assert expected_snippet in body["error"]


@pytest.mark.parametrize("missing", ["channel", "session_key", "message"])
def test_missing_required_field_is_400(monkeypatch, missing):
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != missing}
    body, status = chat.answer(payload)
    assert status == 400


def test_who_is_optional(monkeypatch):
    _stub_no_op_db(monkeypatch)
    monkeypatch.setattr(chat, "_stub_reply", lambda message: "ok")
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "who"}
    body, status = chat.answer(payload)
    assert status == 200


# ── Rate limiting (429) ──────────────────────────────────────────────


def test_rate_limit_429_and_nothing_is_persisted(monkeypatch):
    monkeypatch.setattr(chat, "_recent_user_count", lambda storage_key: 5)
    monkeypatch.setattr(chat, "_insert_message", _raise)  # must not be called

    body, status = chat.answer(VALID_PAYLOAD)

    assert status == 429
    assert body == {
        "ok": False,
        "error": "Rate limit: at most 5 messages per minute per conversation.",
    }


# ── Namespacing ──────────────────────────────────────────────────────


def test_web_and_telegram_share_no_storage_key(monkeypatch):
    storage_keys_seen = []

    def fake_recent_user_count(storage_key):
        storage_keys_seen.append(storage_key)
        return 0

    monkeypatch.setattr(chat, "_recent_user_count", fake_recent_user_count)
    monkeypatch.setattr(chat, "_daily_budget_exceeded", lambda: False)
    monkeypatch.setattr(chat, "_load_history", lambda storage_key, before_id: [])
    monkeypatch.setattr(
        chat, "_insert_message",
        lambda storage_key, channel, who, role, content, **kw: 1,
    )
    monkeypatch.setattr(chat, "_stub_reply", lambda message: "stub reply")

    chat.answer({**VALID_PAYLOAD, "channel": "web", "session_key": "room1"})
    chat.answer({**VALID_PAYLOAD, "channel": "telegram", "session_key": "room1"})

    # Той самий сирий session_key "room1" — але різні canonical-ключі, тож
    # rate-limit/історія веб-виклику фізично не можуть побачити телеграм-сесію.
    assert storage_keys_seen == ["web:room1", "telegram:room1"]


# ── Persist user row before any LLM work ────────────────────────────


def test_user_row_persisted_before_llm_is_attempted(monkeypatch):
    _stub_no_op_db(monkeypatch)
    monkeypatch.setattr(chat, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(chat, "_chat_model_setting", lambda: "claude-sonnet-5")

    persisted_roles = []

    def fake_insert(storage_key, channel, who, role, content, **kw):
        persisted_roles.append(role)
        return len(persisted_roles)

    monkeypatch.setattr(chat, "_insert_message", fake_insert)

    def llm_sees_user_row_already_persisted(messages, model, channel):
        assert persisted_roles == ["user"], (
            "the user row must already be in the database before any LLM "
            "call is attempted"
        )
        return "llm answer", 10, 20

    monkeypatch.setattr(chat, "_llm_reply", llm_sees_user_row_already_persisted)

    body, status = chat.answer(VALID_PAYLOAD)

    assert status == 200
    assert persisted_roles == ["user", "assistant"]
    assert body["tier"] == "llm"


# ── History window repair ────────────────────────────────────────────


def test_repair_window_drops_leading_non_user_rows():
    rows = [
        {"role": "assistant", "content": "orphaned reply"},
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
    ]
    assert chat._repair_window(rows) == [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
    ]


def test_repair_window_drops_trailing_dangling_user_row():
    rows = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "crashed before a reply landed"},
    ]
    assert chat._repair_window(rows) == [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
    ]


def test_repair_window_handles_both_ends_at_once():
    rows = [
        {"role": "assistant", "content": "orphaned"},
        {"role": "user", "content": "a"},
        {"role": "user", "content": "dangling"},
    ]
    assert chat._repair_window(rows) == [{"role": "user", "content": "a"}]


def test_repair_window_empty_input():
    assert chat._repair_window([]) == []


# ── Stub tier: OR-query construction ────────────────────────────────


def test_stub_query_drops_stopwords_and_short_words():
    query = chat._stub_query("What is the Optimism grants program about?")
    assert query == "optimism OR grants OR program"


def test_stub_query_falls_back_to_raw_text_when_nothing_survives():
    message = "is it ok"
    assert chat._stub_query(message) == message[:60]


def test_stub_query_handles_ukrainian_text():
    query = chat._stub_query("Що ви знаєте про гранти Optimism?")
    terms = query.split(" OR ")
    assert "знаєте" in terms
    assert "гранти" in terms
    assert "optimism" in terms
    assert "що" not in terms  # stopword


# ── Stub tier: reply formatting ──────────────────────────────────────


def test_stub_reply_zero_hits_is_honest(monkeypatch):
    monkeypatch.setattr(
        kbtools, "search_impl",
        lambda query, **kw: {"post_hits": [], "topic_title_hits": [], "hint": "x"},
    )
    reply = chat._stub_reply("does the archive know about llamas")
    assert "No matches in the archive" in reply
    assert "ANTHROPIC_API_KEY" in reply


def test_stub_reply_formats_top_hits(monkeypatch):
    hits = [{
        "title": "Grants RFP", "post_url": "https://x/1", "snippet": "«RFP» snippet",
    }]
    monkeypatch.setattr(
        kbtools, "search_impl",
        lambda query, **kw: {"post_hits": hits, "topic_title_hits": [], "hint": "x"},
    )
    reply = chat._stub_reply("grants rfp question")
    assert "1. Grants RFP — https://x/1" in reply
    assert "«RFP» snippet" in reply
    assert "Keyword tier" in reply


# ── Daily budget exceeded ────────────────────────────────────────────


def test_budget_exceeded_forces_stub_and_prepends_note(monkeypatch):
    _stub_no_op_db(monkeypatch, budget_exceeded=True)
    monkeypatch.setattr(chat, "ANTHROPIC_API_KEY", "sk-test")  # would try llm otherwise
    monkeypatch.setattr(chat, "_llm_reply", _raise)  # must never be called
    monkeypatch.setattr(chat, "_stub_reply", lambda message: "keyword answer")

    body, status = chat.answer(VALID_PAYLOAD)

    assert status == 200
    assert body["tier"] == "stub"
    assert body["model"] is None
    assert body["reply_md"] == f"{chat._BUDGET_NOTE}\n\nkeyword answer"


# ── LLM tier: exception -> stub fallback ────────────────────────────


def test_llm_exception_falls_back_to_stub_tier(monkeypatch):
    _stub_no_op_db(monkeypatch)
    monkeypatch.setattr(chat, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(chat, "_chat_model_setting", lambda: "claude-sonnet-5")
    monkeypatch.setattr(chat, "_llm_reply", _raise)
    monkeypatch.setattr(chat, "_stub_reply", lambda message: "stub answer")

    body, status = chat.answer(VALID_PAYLOAD)

    assert status == 200
    assert body["tier"] == "stub"
    assert body["model"] is None
    assert body["tokens"] == {"in": None, "out": None}
    assert body["reply_md"] == "stub answer"


def test_no_api_key_never_attempts_llm(monkeypatch):
    _stub_no_op_db(monkeypatch)
    assert chat.ANTHROPIC_API_KEY == ""  # module default from env, set above
    monkeypatch.setattr(chat, "_llm_reply", _raise)
    monkeypatch.setattr(chat, "_stub_reply", lambda message: "stub answer")

    body, status = chat.answer(VALID_PAYLOAD)

    assert status == 200
    assert body["tier"] == "stub"


# ── LLM tier: the agentic loop itself (_llm_reply) ──────────────────


def test_llm_reply_happy_path_with_one_tool_call(monkeypatch):
    monkeypatch.setattr(
        kbtools, "search_impl",
        lambda query, **kw: {
            "post_hits": [{"title": "T", "post_url": "https://x/1", "snippet": "s"}],
            "topic_title_hits": [], "hint": "h",
        },
    )
    responses = [
        FakeResponse([tool_use_block("search_kb", {"query": "grants"}, "call_1")], "tool_use"),
        FakeResponse([text_block("Answer.\n\nSources:\n1. https://x/1")], "end_turn",
                     tokens_in=20, tokens_out=30),
    ]
    holder = install_fake_anthropic(monkeypatch, responses)

    text, tokens_in, tokens_out = chat._llm_reply(
        [{"role": "user", "content": "any grants for dev tooling?"}], "claude-sonnet-5", "web",
    )

    assert "Sources:" in text
    assert tokens_in == 30   # 10 + 20
    assert tokens_out == 35  # 5 + 30
    second_call_messages = holder["client"].messages.calls[1]["messages"]
    tool_result = second_call_messages[-1]["content"][0]
    assert tool_result["tool_use_id"] == "call_1"
    assert "is_error" not in tool_result


def test_llm_reply_tool_error_is_fed_back_as_is_error(monkeypatch):
    monkeypatch.setattr(kbtools, "search_impl", _raise)
    responses = [
        FakeResponse([tool_use_block("search_kb", {"query": "x"}, "call_1")], "tool_use"),
        FakeResponse([text_block("recovered")], "end_turn"),
    ]
    holder = install_fake_anthropic(monkeypatch, responses)

    text, *_ = chat._llm_reply([{"role": "user", "content": "q"}], "m", "web")

    assert text == "recovered"
    tool_result = holder["client"].messages.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "boom" in tool_result["content"]


def test_llm_reply_refusal_returns_polite_text(monkeypatch):
    responses = [FakeResponse([], "refusal")]
    install_fake_anthropic(monkeypatch, responses)

    text, *_ = chat._llm_reply([{"role": "user", "content": "q"}], "m", "web")

    assert text == chat._REFUSAL_TEXT


def test_llm_reply_max_tokens_appends_truncation_note(monkeypatch):
    responses = [FakeResponse([text_block("partial answer")], "max_tokens")]
    install_fake_anthropic(monkeypatch, responses)

    text, *_ = chat._llm_reply([{"role": "user", "content": "long question"}], "m", "web")

    assert text.startswith("partial answer")
    assert "truncated" in text


def test_llm_reply_iteration_cap_without_text_raises(monkeypatch):
    monkeypatch.setattr(kbtools, "search_impl", lambda query, **kw: {"post_hits": []})
    responses = [
        FakeResponse([tool_use_block("search_kb", {"query": "x"}, f"call_{i}")], "tool_use")
        for i in range(chat.MAX_TOOL_ITERATIONS)
    ]
    install_fake_anthropic(monkeypatch, responses)

    with pytest.raises(RuntimeError):
        chat._llm_reply([{"role": "user", "content": "q"}], "m", "web")


def test_llm_reply_iteration_cap_with_trailing_text_returns_it(monkeypatch):
    monkeypatch.setattr(kbtools, "search_impl", lambda query, **kw: {"post_hits": []})
    responses = [
        FakeResponse(
            [text_block("still working"), tool_use_block("search_kb", {"query": "x"}, f"call_{i}")],
            "tool_use",
        )
        for i in range(chat.MAX_TOOL_ITERATIONS)
    ]
    install_fake_anthropic(monkeypatch, responses)

    text, *_ = chat._llm_reply([{"role": "user", "content": "q"}], "m", "web")

    assert text == "still working"


def test_llm_reply_telegram_channel_adds_plain_text_system_block(monkeypatch):
    responses = [FakeResponse([text_block("ok")], "end_turn")]
    holder = install_fake_anthropic(monkeypatch, responses)

    chat._llm_reply([{"role": "user", "content": "q"}], "m", "telegram")

    system = holder["client"].messages.calls[0]["system"]
    assert len(system) == 2
    assert system[1]["text"] == chat._TELEGRAM_SYSTEM
    assert "cache_control" not in system[1]


def test_llm_reply_web_channel_has_no_telegram_block(monkeypatch):
    responses = [FakeResponse([text_block("ok")], "end_turn")]
    holder = install_fake_anthropic(monkeypatch, responses)

    chat._llm_reply([{"role": "user", "content": "q"}], "m", "web")

    system = holder["client"].messages.calls[0]["system"]
    assert len(system) == 1
    assert system[0]["cache_control"] == {"type": "ephemeral"}


# ── Tool dispatch ──────────────────────────────────────────────────


def test_dispatch_tool_caps_get_topic_max_posts_at_60(monkeypatch):
    captured = {}

    def fake_topic_impl(forum, topic_id, offset=0, max_posts=60):
        captured["max_posts"] = max_posts
        return {"title": "t", "posts": []}

    monkeypatch.setattr(kbtools, "topic_impl", fake_topic_impl)

    chat._dispatch_tool("get_topic", {"forum": "optimism", "topic_id": 1, "max_posts": 500})

    assert captured["max_posts"] == 60


def test_dispatch_tool_unknown_name_returns_error_json():
    import json

    payload = json.loads(chat._dispatch_tool("mystery_tool", {}))
    assert "error" in payload
