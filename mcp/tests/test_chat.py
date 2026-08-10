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
    FakeResponse, install as install_fake_anthropic, server_tool_use_block,
    text_block, tool_use_block, web_search_tool_result_block,
)
from fakedb import make_db  # noqa: E402

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
    # web_search — окрема settings-читалка (agent 2.0); тести, яким байдужий
    # web_search, не мають торкатися БД заради неї.
    monkeypatch.setattr(chat, "_web_search_enabled", lambda: False)


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

    def llm_sees_user_row_already_persisted(messages, model, channel, **kw):
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
    # Режим називається ПЕРШИМ рядком, до результатів (живий урок 2026-08-07:
    # футер дрібним шрифтом ніхто не читає — «Ти уже працюєш?» → 5 випадкових
    # лінків → «зламано»).
    assert reply.startswith("🔎 Keyword mode")


def test_stub_reply_without_latin_keywords_explains_instead_of_searching(monkeypatch):
    """Питання без жодного латинського слова (архів англомовний!) не має
    йти в пошук — випадкові збіги виглядають як поломка. Чесне пояснення
    режиму + приклади запитів, і НУЛЬ звернень до search_impl."""
    def _boom(*a, **kw):
        raise AssertionError("search_impl не мав викликатися")

    monkeypatch.setattr(kbtools, "search_impl", _boom)
    reply = chat._stub_reply("Ти уже працюєш?")
    assert "Keyword mode" in reply
    assert "any language" in reply  # обіцянка агентного рівня
    assert "Optimism" in reply      # приклади ключових слів


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


# ── Отруєння історії stub-ерою (знайдено наживо 2026-08-10) ──────────


def test_drop_stub_pairs_removes_stub_answer_with_its_question():
    """Модель читала у власній історії stub-банери «AI tier is not enabled»
    і відповідала «доступу нема» ВЖЕ МАЮЧИ доступ. Stub-пара (питання +
    keyword-відповідь) викидається цілком — інакше ламається чергування."""
    rows = [
        {"role": "user", "content": "old q", "tier": None},
        {"role": "assistant", "content": "🔎 Keyword mode — not enabled", "tier": "stub"},
        {"role": "user", "content": "real q", "tier": None},
        {"role": "assistant", "content": "real answer", "tier": "llm"},
    ]
    out = chat._drop_stub_pairs(rows)
    assert [(r["role"], r["content"]) for r in out] == [
        ("user", "real q"), ("assistant", "real answer"),
    ]


def test_drop_stub_pairs_handles_orphan_stub_and_keeps_llm_only_history():
    rows = [
        {"role": "assistant", "content": "orphan stub", "tier": "stub"},
        {"role": "user", "content": "q", "tier": None},
        {"role": "assistant", "content": "a", "tier": "llm"},
    ]
    out = chat._drop_stub_pairs(rows)
    assert [(r["role"]) for r in out] == ["user", "assistant"]
    assert chat._drop_stub_pairs([]) == []


# ── Agent 2.0: identity line ────────────────────────────────────────


def test_identity_line_present_in_system_prompt():
    """Живий урок 2026-08-10 (_drop_stub_pairs): модель мусить знати, що
    вона Й Є AI-рівнем — інакше на «ти маєш доступ до Anthropic?» відповідає
    буквально «ні», бо ніколи не бачила прямого підтвердження."""
    assert "live AI tier" in chat._CHAT_SYSTEM
    assert "Anthropic" in chat._CHAT_SYSTEM
    assert "predate the key" in chat._CHAT_SYSTEM


def test_list_findings_system_line_present():
    assert "list_findings" in chat._CHAT_SYSTEM
    assert "not the public forum archive" in chat._CHAT_SYSTEM


# ── Agent 2.0: web_search gated by settings ─────────────────────────


def test_llm_reply_web_search_off_by_default(monkeypatch):
    """Дефолт web_search=False (як і в answer(), коли _web_search_enabled()
    поверне 'off') відтворює РІВНО сьогоднішню поведінку: жодного
    server-tool у tools, жодного зайвого system-рядка."""
    responses = [FakeResponse([text_block("ok")], "end_turn")]
    holder = install_fake_anthropic(monkeypatch, responses)

    chat._llm_reply([{"role": "user", "content": "q"}], "m", "web")

    tools = holder["client"].messages.calls[0]["tools"]
    assert len(tools) == len(chat._TOOLS)
    assert all(t["name"] != "web_search" for t in tools)
    system = holder["client"].messages.calls[0]["system"]
    assert len(system) == 1
    assert chat._WEB_SEARCH_SYSTEM_LINE not in [b["text"] for b in system]


def test_llm_reply_web_search_on_adds_server_tool_and_system_line(monkeypatch):
    responses = [FakeResponse([text_block("ok")], "end_turn")]
    holder = install_fake_anthropic(monkeypatch, responses)

    chat._llm_reply([{"role": "user", "content": "q"}], "m", "web", web_search=True)

    tools = holder["client"].messages.calls[0]["tools"]
    assert len(tools) == len(chat._TOOLS) + 1
    assert tools[-1] == chat._WEB_SEARCH_TOOL
    assert tools[-1]["type"] == "web_search_20260318"
    assert tools[-1]["max_uses"] == 3
    system = holder["client"].messages.calls[0]["system"]
    assert chat._WEB_SEARCH_SYSTEM_LINE in [b["text"] for b in system]


def test_llm_reply_web_search_on_telegram_keeps_both_extra_system_lines(monkeypatch):
    responses = [FakeResponse([text_block("ok")], "end_turn")]
    holder = install_fake_anthropic(monkeypatch, responses)

    chat._llm_reply(
        [{"role": "user", "content": "q"}], "m", "telegram", web_search=True,
    )

    system_texts = [b["text"] for b in holder["client"].messages.calls[0]["system"]]
    assert chat._WEB_SEARCH_SYSTEM_LINE in system_texts
    assert chat._TELEGRAM_SYSTEM in system_texts


def test_answer_reads_web_search_setting_and_passes_it_through(monkeypatch):
    """answer() must ask _web_search_enabled() (not hardcode False) and pass
    the result into _llm_reply — this is the only wiring test_llm_reply's
    own unit tests can't cover, since they call _llm_reply directly."""
    _stub_no_op_db(monkeypatch)
    monkeypatch.setattr(chat, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(chat, "_chat_model_setting", lambda: "claude-sonnet-5")
    monkeypatch.setattr(chat, "_web_search_enabled", lambda: True)

    seen = {}

    def fake_llm_reply(messages, model, channel, web_search=False):
        seen["web_search"] = web_search
        return "answer", 1, 1

    monkeypatch.setattr(chat, "_llm_reply", fake_llm_reply)

    body, status = chat.answer(VALID_PAYLOAD)

    assert status == 200
    assert seen["web_search"] is True


# ── Agent 2.0: mixed content with a server-tool block ───────────────


def test_llm_reply_mixed_server_tool_content_end_turn_does_not_crash(monkeypatch):
    """server_tool_use/web_search_tool_result blocks must not break the
    text-extraction pass (block.type != 'text' → skipped, exactly like
    tool_use blocks already were before this change)."""
    responses = [
        FakeResponse(
            [
                text_block("Searching the web."),
                server_tool_use_block("web_search", {"query": "optimism grants 2026"}),
                web_search_tool_result_block(
                    content=[{"type": "web_search_result", "url": "https://x", "title": "t"}],
                ),
                text_block("Found it.\n\nSources:\n1. https://x"),
            ],
            "end_turn",
        ),
    ]
    install_fake_anthropic(monkeypatch, responses)

    text, *_ = chat._llm_reply(
        [{"role": "user", "content": "any recent news?"}], "m", "web", web_search=True,
    )

    assert text == "Searching the web.\nFound it.\n\nSources:\n1. https://x"


def test_llm_reply_mixed_server_tool_and_client_tool_use_in_same_turn(monkeypatch):
    """A response can carry BOTH a resolved server-tool pair (web_search) AND
    a pending client tool_use (search_kb) in the same content list, with
    stop_reason='tool_use' driven by the client tool alone. The dispatch
    loop must only act on the tool_use block — server-tool blocks are
    echoed back verbatim (their encrypted_content matters for replay) but
    never locally dispatched."""
    monkeypatch.setattr(
        kbtools, "search_impl",
        lambda query, **kw: {"post_hits": [], "topic_title_hits": [], "hint": "h"},
    )
    responses = [
        FakeResponse(
            [
                text_block("Checking both."),
                server_tool_use_block("web_search", {"query": "x"}, "srv_1"),
                web_search_tool_result_block(id_="srv_1", content=[]),
                tool_use_block("search_kb", {"query": "grants"}, "call_1"),
            ],
            "tool_use",
        ),
        FakeResponse([text_block("done")], "end_turn"),
    ]
    holder = install_fake_anthropic(monkeypatch, responses)

    text, *_ = chat._llm_reply(
        [{"role": "user", "content": "q"}], "m", "web", web_search=True,
    )

    assert text == "done"
    second_call_messages = holder["client"].messages.calls[1]["messages"]
    tool_results = second_call_messages[-1]["content"]
    # Лише клієнтський tool_use породив tool_result — server-tool блоки
    # мовчки пропущені, не здиспетчерені.
    assert len(tool_results) == 1
    assert tool_results[0]["tool_use_id"] == "call_1"
    # Асистентський хід, який пішов назад у messages, і досі несе
    # server-tool блоки незмінними (потрібні для реплею на наступний хід).
    assistant_turn = second_call_messages[-2]
    assert assistant_turn["role"] == "assistant"
    block_types = [b.type for b in assistant_turn["content"]]
    assert "server_tool_use" in block_types
    assert "web_search_tool_result" in block_types


# ── list_findings dispatch ───────────────────────────────────────────


def test_dispatch_tool_list_findings_calls_findings_impl_with_defaults(monkeypatch):
    captured = {}

    def fake_findings_impl(**kw):
        captured.update(kw)
        return {"findings": []}

    monkeypatch.setattr(kbtools, "findings_impl", fake_findings_impl)

    chat._dispatch_tool("list_findings", {"ecosystem": "Optimism"})

    assert captured == {
        "ecosystem": "Optimism", "status": None, "days": 14,
        "min_confidence": None, "limit": 10,
    }


def test_dispatch_tool_list_findings_passes_through_all_fields(monkeypatch):
    captured = {}

    def fake_findings_impl(**kw):
        captured.update(kw)
        return {"findings": []}

    monkeypatch.setattr(kbtools, "findings_impl", fake_findings_impl)

    chat._dispatch_tool("list_findings", {
        "ecosystem": "Arbitrum", "status": "done", "days": 30,
        "min_confidence": 0.8, "limit": 5,
    })

    assert captured == {
        "ecosystem": "Arbitrum", "status": "done", "days": 30,
        "min_confidence": 0.8, "limit": 5,
    }


# ── /keywords-advice (chat.keywords_advice) ─────────────────────────


def test_keywords_advice_fail_closed_when_token_unset(monkeypatch):
    monkeypatch.delenv("KB_MCP_TOKEN", raising=False)
    body, status = chat.keywords_advice()
    assert status == 403
    assert body == {"ok": False, "error": "Chat is disabled: KB_MCP_TOKEN is not set."}


def test_keywords_advice_no_key_returns_503(monkeypatch):
    assert chat.ANTHROPIC_API_KEY == ""  # module default, no key configured
    body, status = chat.keywords_advice()
    assert status == 503
    assert body == {"ok": False, "error": "AI tier is off — set ANTHROPIC_API_KEY."}


def test_keywords_advice_happy_path(monkeypatch):
    monkeypatch.setattr(chat, "ANTHROPIC_API_KEY", "sk-test")

    router = [
        ("SELECT value FROM settings", [{"value": "claude-sonnet-5"}]),
        ("FROM keywords", [{"pattern": "grant", "kind": "include"}]),
        ("status = 'filtered'", [{"title": "unrelated noise thread"}]),
        ("status = 'done'", [{"category": "FUNDING", "n": 3}]),
    ]
    db_factory, _calls = make_db(router)
    monkeypatch.setattr(kbtools, "_db", db_factory)

    responses = [FakeResponse(
        [text_block("1. Add 'bounty' — recurring in FUNDING titles.")], "end_turn",
    )]
    holder = install_fake_anthropic(monkeypatch, responses)

    body, status = chat.keywords_advice()

    assert status == 200
    assert body["ok"] is True
    assert body["model"] == "claude-sonnet-5"
    assert "bounty" in body["advice_md"]

    call = holder["client"].messages.calls[0]
    assert call["model"] == "claude-sonnet-5"
    assert call["max_tokens"] == 1200
    assert "tools" not in call  # no-tools single call, per spec
    prompt = call["messages"][0]["content"]
    assert "[include] grant" in prompt
    assert "unrelated noise thread" in prompt
    assert "FUNDING: 3" in prompt
