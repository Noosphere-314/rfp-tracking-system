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
        ({"web": "yes"}, "web must be a boolean"),
        ({"web": 1}, "web must be a boolean"),
        ({"web": 0}, "web must be a boolean"),
        ({"web": None}, "web must be a boolean"),  # present-but-null, not absent
        ({"web": []}, "web must be a boolean"),
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


# ── A: per-request web flag (payload.web OR settings.chat_web_search) ──


@pytest.mark.parametrize("web_value", [True, False])
def test_web_true_and_false_pass_validation(monkeypatch, web_value):
    _stub_no_op_db(monkeypatch)
    monkeypatch.setattr(chat, "_stub_reply", lambda message: "ok")
    payload = {**VALID_PAYLOAD, "web": web_value}
    body, status = chat.answer(payload)
    assert status == 200


def test_web_true_enables_search_even_when_settings_off(monkeypatch):
    """payload web=true must add the server tool even with chat_web_search
    globally 'off' — and short-circuits the settings read entirely (the
    payload already answers the question, no need to hit the DB for it)."""
    _stub_no_op_db(monkeypatch)
    monkeypatch.setattr(chat, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(chat, "_chat_model_setting", lambda: "claude-sonnet-5")
    monkeypatch.setattr(chat, "_web_search_enabled", _raise)  # must not be called

    seen = {}

    def fake_llm_reply(messages, model, channel, web_search=False):
        seen["web_search"] = web_search
        return "answer", 1, 1

    monkeypatch.setattr(chat, "_llm_reply", fake_llm_reply)

    body, status = chat.answer({**VALID_PAYLOAD, "web": True})

    assert status == 200
    assert seen["web_search"] is True


def test_web_absent_and_settings_off_disables_search(monkeypatch):
    _stub_no_op_db(monkeypatch)  # _web_search_enabled → False
    monkeypatch.setattr(chat, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(chat, "_chat_model_setting", lambda: "claude-sonnet-5")

    seen = {}

    def fake_llm_reply(messages, model, channel, web_search=False):
        seen["web_search"] = web_search
        return "answer", 1, 1

    monkeypatch.setattr(chat, "_llm_reply", fake_llm_reply)

    body, status = chat.answer(VALID_PAYLOAD)  # no "web" key at all

    assert status == 200
    assert seen["web_search"] is False


def test_web_false_and_settings_off_disables_search(monkeypatch):
    _stub_no_op_db(monkeypatch)
    monkeypatch.setattr(chat, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(chat, "_chat_model_setting", lambda: "claude-sonnet-5")

    seen = {}

    def fake_llm_reply(messages, model, channel, web_search=False):
        seen["web_search"] = web_search
        return "answer", 1, 1

    monkeypatch.setattr(chat, "_llm_reply", fake_llm_reply)

    body, status = chat.answer({**VALID_PAYLOAD, "web": False})

    assert status == 200
    assert seen["web_search"] is False


def test_web_false_but_settings_on_still_enables_search(monkeypatch):
    """The OR is settings-inclusive, not payload-exclusive: an explicit
    web=false in one request must not turn off a globally-enabled setting."""
    _stub_no_op_db(monkeypatch)
    monkeypatch.setattr(chat, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(chat, "_chat_model_setting", lambda: "claude-sonnet-5")
    monkeypatch.setattr(chat, "_web_search_enabled", lambda: True)

    seen = {}

    def fake_llm_reply(messages, model, channel, web_search=False):
        seen["web_search"] = web_search
        return "answer", 1, 1

    monkeypatch.setattr(chat, "_llm_reply", fake_llm_reply)

    body, status = chat.answer({**VALID_PAYLOAD, "web": False})

    assert status == 200
    assert seen["web_search"] is True


def test_response_contract_unchanged_with_web_flag(monkeypatch):
    _stub_no_op_db(monkeypatch)
    monkeypatch.setattr(chat, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(chat, "_chat_model_setting", lambda: "claude-sonnet-5")
    monkeypatch.setattr(chat, "_llm_reply", lambda *a, **kw: ("answer", 5, 7))

    body, status = chat.answer({**VALID_PAYLOAD, "web": True})

    assert status == 200
    assert set(body.keys()) == {"ok", "reply_md", "tier", "model", "tokens"}



def test_llm_reply_web_search_off_has_no_hint_line(monkeypatch):
    responses = [FakeResponse([text_block("ok")], "end_turn")]
    holder = install_fake_anthropic(monkeypatch, responses)

    chat._llm_reply([{"role": "user", "content": "q"}], "m", "web")

    system_texts = [b["text"] for b in holder["client"].messages.calls[0]["system"]]
    assert chat._WEB_SEARCH_HINT_LINE not in system_texts


# ── Agent 2.0: mixed content with a server-tool block ───────────────




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


# ── B: /chat-brief (chat.chat_brief) ─────────────────────────────────


CHAT_BRIEF_PAYLOAD = {"channel": "web", "session_key": "sess-1"}


def _chat_brief_settings_db(monkeypatch, model="claude-opus-5", max_words="350"):
    """Fakes the `with kbtools._db() as conn: _setting(...); _brief_max_words(...)`
    block inside chat_brief — same SQL text for both reads (SELECT value FROM
    settings WHERE key = %s), branching by the key param is what fakedb's
    callable-response form is for."""
    def settings_response(params):
        key = params[0]
        if key == "brief_model":
            return [{"value": model}]
        if key == "brief_max_words":
            return [{"value": max_words}]
        return []

    db_factory, calls = make_db([("SELECT value FROM settings", settings_response)])
    monkeypatch.setattr(kbtools, "_db", db_factory)
    return calls


def test_chat_brief_fail_closed_when_token_unset(monkeypatch):
    monkeypatch.delenv("KB_MCP_TOKEN", raising=False)
    body, status = chat.chat_brief(CHAT_BRIEF_PAYLOAD)
    assert status == 403
    assert body == {"ok": False, "error": "Chat is disabled: KB_MCP_TOKEN is not set."}


def test_chat_brief_fail_closed_takes_priority_over_contract_validation(monkeypatch):
    monkeypatch.setenv("KB_MCP_TOKEN", "")
    body, status = chat.chat_brief({"garbage": True})
    assert status == 403


@pytest.mark.parametrize(
    "overrides,expected_snippet",
    [
        ({"channel": "sms"}, "channel"),
        ({"channel": None}, "channel"),
        ({"session_key": ""}, "session_key is required"),
        ({"session_key": None}, "session_key is required"),
        ({"session_key": "a" * 161}, "at most 160"),
        ({"session_key": "web:room1"}, "must not contain"),
    ],
)
def test_chat_brief_contract_validation_400(monkeypatch, overrides, expected_snippet):
    payload = {**CHAT_BRIEF_PAYLOAD, **overrides}
    body, status = chat.chat_brief(payload)
    assert status == 400
    assert body["ok"] is False
    assert expected_snippet in body["error"]


def test_chat_brief_contract_ignores_who_and_message(monkeypatch):
    """/chat-brief's contract is deliberately the channel+session_key SUBSET
    of /chat's (see _validate_channel_and_session) — who/message rules from
    _validate don't apply here, there's no message in this request."""
    monkeypatch.setattr(chat, "_load_all_messages", lambda storage_key: [])
    body, status = chat.chat_brief({**CHAT_BRIEF_PAYLOAD, "who": "x" * 999, "message": 123})
    assert status == 404  # sailed past validation straight to the row-count check


def test_chat_brief_storage_key_is_channel_prefixed(monkeypatch):
    seen = {}

    def fake_load(storage_key):
        seen["storage_key"] = storage_key
        return []

    monkeypatch.setattr(chat, "_load_all_messages", fake_load)

    chat.chat_brief({"channel": "telegram", "session_key": "room1"})

    assert seen["storage_key"] == "telegram:room1"


def test_chat_brief_zero_rows_404(monkeypatch):
    monkeypatch.setattr(chat, "_load_all_messages", lambda storage_key: [])
    body, status = chat.chat_brief(CHAT_BRIEF_PAYLOAD)
    assert status == 404
    assert body == {"ok": False, "error": "Nothing to summarize in this conversation yet."}


def test_chat_brief_one_row_404(monkeypatch):
    monkeypatch.setattr(
        chat, "_load_all_messages",
        lambda storage_key: [{"role": "user", "content": "only one message", "tier": None}],
    )
    body, status = chat.chat_brief(CHAT_BRIEF_PAYLOAD)
    assert status == 404


def test_chat_brief_no_api_key_returns_503(monkeypatch):
    rows = [
        {"role": "user", "content": "q", "tier": None},
        {"role": "assistant", "content": "a", "tier": "llm"},
    ]
    monkeypatch.setattr(chat, "_load_all_messages", lambda storage_key: rows)
    assert chat.ANTHROPIC_API_KEY == ""  # module default, no key configured

    body, status = chat.chat_brief(CHAT_BRIEF_PAYLOAD)

    assert status == 503
    assert body == {"ok": False, "error": "AI tier is off — set ANTHROPIC_API_KEY."}


def test_chat_brief_row_count_checked_before_api_key(monkeypatch):
    """404 (nothing to summarize) must win over 503 (no key) — matches the
    spec's stated check order, and is the more useful error for an empty
    conversation regardless of whether the AI tier is configured."""
    monkeypatch.setattr(chat, "_load_all_messages", lambda storage_key: [])
    assert chat.ANTHROPIC_API_KEY == ""

    body, status = chat.chat_brief(CHAT_BRIEF_PAYLOAD)

    assert status == 404


def test_chat_brief_llm_exception_returns_502_no_stub_fallback(monkeypatch):
    rows = [
        {"role": "user", "content": "q", "tier": None},
        {"role": "assistant", "content": "a", "tier": "llm"},
    ]
    monkeypatch.setattr(chat, "_load_all_messages", lambda storage_key: rows)
    monkeypatch.setattr(chat, "ANTHROPIC_API_KEY", "sk-test")
    _chat_brief_settings_db(monkeypatch)
    monkeypatch.setattr(chat, "_insert_brief", _raise)  # must not be reached

    install_fake_anthropic(monkeypatch, [RuntimeError("boom")])

    body, status = chat.chat_brief(CHAT_BRIEF_PAYLOAD)

    assert status == 502
    assert body == {"ok": False, "error": "Report generation failed — try again."}


def test_chat_brief_title_truncated_to_90_chars(monkeypatch):
    long_message = "Q" * 120
    rows = [
        {"role": "user", "content": long_message, "tier": None},
        {"role": "assistant", "content": "a", "tier": "llm"},
    ]
    monkeypatch.setattr(chat, "_load_all_messages", lambda storage_key: rows)
    monkeypatch.setattr(chat, "ANTHROPIC_API_KEY", "sk-test")
    _chat_brief_settings_db(monkeypatch)
    monkeypatch.setattr(chat, "_insert_brief", lambda *a, **kw: 1)
    install_fake_anthropic(monkeypatch, [FakeResponse([text_block("report")], "end_turn")])

    body, status = chat.chat_brief(CHAT_BRIEF_PAYLOAD)

    assert status == 200
    assert body["title"] == long_message[:90]
    assert len(body["title"]) == 90


def test_chat_brief_title_uses_first_user_message_not_first_row(monkeypatch):
    rows = [
        {"role": "assistant", "content": "orphaned lead-in", "tier": "llm"},
        {"role": "user", "content": "the real opening question", "tier": None},
        {"role": "assistant", "content": "a", "tier": "llm"},
    ]
    monkeypatch.setattr(chat, "_load_all_messages", lambda storage_key: rows)
    monkeypatch.setattr(chat, "ANTHROPIC_API_KEY", "sk-test")
    _chat_brief_settings_db(monkeypatch)
    monkeypatch.setattr(chat, "_insert_brief", lambda *a, **kw: 1)
    install_fake_anthropic(monkeypatch, [FakeResponse([text_block("report")], "end_turn")])

    body, status = chat.chat_brief(CHAT_BRIEF_PAYLOAD)

    assert status == 200
    assert body["title"] == "the real opening question"


def test_load_all_messages_drops_stub_pairs_and_caps_at_200(monkeypatch):
    raw_rows = [
        {"role": "user", "content": "old q", "tier": None},
        {"role": "assistant", "content": "keyword stub", "tier": "stub"},
        {"role": "user", "content": "real q", "tier": None},
        {"role": "assistant", "content": "real a", "tier": "llm"},
    ]
    db_factory, calls = make_db([("kb.chat_messages", raw_rows)])
    monkeypatch.setattr(kbtools, "_db", db_factory)

    rows = chat._load_all_messages("web:sess-1")

    assert [(r["role"], r["content"]) for r in rows] == [
        ("user", "real q"), ("assistant", "real a"),
    ]
    sql, params = calls[0]
    assert "ORDER BY id LIMIT 200" in sql  # spec: capped at 200 rows, ascending
    assert params == ("web:sess-1",)


def test_insert_brief_uses_chat_ecosystem_and_null_item_uid(monkeypatch):
    db_factory, calls = make_db([("INSERT INTO kb.briefs", [{"id": 55}])])
    monkeypatch.setattr(kbtools, "_db", db_factory)

    brief_id = chat._insert_brief("Title", "Body md", "claude-opus-5", 10, 20)

    assert brief_id == 55
    sql, params = calls[0]
    assert "INSERT INTO kb.briefs" in sql
    assert params == (None, "chat", "Title", "Body md", "llm", "claude-opus-5", 10, 20)


def test_chat_brief_happy_path_end_to_end(monkeypatch):
    monkeypatch.setattr(chat, "ANTHROPIC_API_KEY", "sk-test")

    chat_rows = [
        {"role": "user", "content": "What grants has Optimism funded for dev tooling?",
         "tier": None},
        {"role": "assistant", "content": "Answer one.\n\nSources:\n1. https://x/1",
         "tier": "llm"},
        {"role": "user", "content": "Any recent ones?", "tier": None},
        {"role": "assistant", "content": "Answer two.\n\nSources:\n1. https://x/2",
         "tier": "llm"},
    ]

    def settings_response(params):
        key = params[0]
        if key == "brief_model":
            return [{"value": "claude-opus-5"}]
        if key == "brief_max_words":
            return [{"value": "500"}]
        return []

    router = [
        ("kb.chat_messages", chat_rows),
        ("SELECT value FROM settings", settings_response),
        ("INSERT INTO kb.briefs", [{"id": 77}]),
    ]
    db_factory, calls = make_db(router)
    monkeypatch.setattr(kbtools, "_db", db_factory)

    report_text = "### What was investigated\n...\n\nSources:\n1. https://x/1"
    responses = [FakeResponse([text_block(report_text)], "end_turn",
                               tokens_in=100, tokens_out=200)]
    holder = install_fake_anthropic(monkeypatch, responses)

    body, status = chat.chat_brief({"channel": "web", "session_key": "sess-1"})

    assert status == 200
    assert body == {
        "ok": True, "brief_id": 77,
        "title": "What grants has Optimism funded for dev tooling?",
    }

    call = holder["client"].messages.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["max_tokens"] == 3000
    assert "tools" not in call  # single one-shot call, no agentic loop
    assert "500" in call["system"]  # C: brief_max_words injected per-call
    transcript = call["messages"][0]["content"]
    assert "CONVERSATION TRANSCRIPT" in transcript
    assert "What grants has Optimism funded for dev tooling?" in transcript
    assert "Answer two." in transcript

    insert_call = next(c for c in calls if "INSERT INTO kb.briefs" in c[0])
    assert insert_call[1] == (
        None, "chat", "What grants has Optimism funded for dev tooling?",
        report_text, "llm", "claude-opus-5", 100, 200,
    )


# ── C: brief_max_words clamp (chat._brief_max_words) ─────────────────


def test_brief_max_words_clamps_below_floor():
    db_factory, _ = make_db([("SELECT value FROM settings", [{"value": "10"}])])
    assert chat._brief_max_words(db_factory()) == 100


def test_brief_max_words_clamps_above_ceiling():
    db_factory, _ = make_db([("SELECT value FROM settings", [{"value": "9999"}])])
    assert chat._brief_max_words(db_factory()) == 2000


def test_brief_max_words_non_numeric_falls_back_to_350():
    db_factory, _ = make_db([("SELECT value FROM settings", [{"value": "lots"}])])
    assert chat._brief_max_words(db_factory()) == 350


def test_brief_max_words_missing_setting_falls_back_to_350():
    db_factory, _ = make_db([])  # no row → _setting's own default kicks in
    assert chat._brief_max_words(db_factory()) == 350


def test_brief_max_words_within_range_passes_through():
    db_factory, _ = make_db([("SELECT value FROM settings", [{"value": "500"}])])
    assert chat._brief_max_words(db_factory()) == 500


# ── A2: авто-детект «перевір в інтернеті» (запит Миколи 2026-08-11) ───


def test_wants_web_detects_bilingual_verification_cues():
    for msg in ["перевір це в інтернеті", "загугли останні новини",
                "verify this claim online", "what's the latest on GG24",
                "покажи актуальні дедлайни", "double-check the amount"]:
        assert chat._wants_web(msg) is True, msg
    for msg in ["що фінансував Optimism у dev tooling?",
                "summarize Compound grants", "хто вирішує в Aave"]:
        assert chat._wants_web(msg) is False, msg


def test_verification_hint_forces_web_even_with_toggle_and_setting_off(monkeypatch):
    """Ключова вимога: тумблер вимкнений, глобальний параметр вимкнений, але в
    самому питанні є «перевір в інтернеті» → web_search все одно вмикається."""
    _stub_no_op_db(monkeypatch)
    monkeypatch.setattr(chat, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(chat, "_chat_model_setting", lambda: "claude-sonnet-5")
    monkeypatch.setattr(chat, "_web_search_enabled", lambda: False)

    seen = {}

    def fake_llm_reply(messages, model, channel, web_search=False):
        seen["web_search"] = web_search
        return "answer", 1, 1

    monkeypatch.setattr(chat, "_llm_reply", fake_llm_reply)
    body, status = chat.answer(
        {**VALID_PAYLOAD, "message": "перевір в інтернеті останні гранти Optimism"}
    )
    assert status == 200
    assert seen["web_search"] is True


def test_telegram_always_gets_web_search(monkeypatch):
    """Рішення Миколи 2026-08-11: у Telegram інтернет увімкнений завжди —
    жодних команд-тумблерів; чекбокс лишається тільки у вебі."""
    _stub_no_op_db(monkeypatch)
    monkeypatch.setattr(chat, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(chat, "_chat_model_setting", lambda: "claude-sonnet-5")
    monkeypatch.setattr(chat, "_web_search_enabled", _raise)  # не має читатись

    seen = {}

    def fake_llm_reply(messages, model, channel, web_search=False):
        seen["web_search"] = web_search
        return "answer", 1, 1

    monkeypatch.setattr(chat, "_llm_reply", fake_llm_reply)
    body, status = chat.answer(
        {**VALID_PAYLOAD, "channel": "telegram", "message": "що там по грантах"}
    )
    assert status == 200
    assert seen["web_search"] is True


# ── Веб-пошук ОКРЕМИМ предкроком (редизайн 2026-08-11) ───────────────


def test_web_search_runs_as_a_separate_call_without_local_tools(monkeypatch):
    """Змішувати серверний пошук із локальними інструментами в одному ході
    не можна (два взаємовиключні 400 від API + зациклення). Тому пошук —
    окремий виклик БЕЗ _TOOLS, а його текст іде в цикл контекстом."""
    responses = [
        FakeResponse([text_block("Web says: program is open, deadline Sep 1.")], "end_turn"),
        FakeResponse([text_block("answer")], "end_turn"),
    ]
    holder = install_fake_anthropic(monkeypatch, responses)

    text, *_ = chat._llm_reply(
        [{"role": "user", "content": "check online: compound grants?"}],
        "m", "web", web_search=True,
    )

    calls = holder["client"].messages.calls
    assert len(calls) == 2, "має бути предкрок + основний виклик"

    web_call = calls[0]
    assert [t["name"] for t in web_call["tools"]] == ["web_search"]
    assert web_call["tools"][0]["type"] == "web_search_20260209"

    main_call = calls[1]
    tool_names = [t.get("name") for t in main_call["tools"]]
    assert "web_search" not in tool_names, "у циклі веб-інструмента бути не має"
    assert "search_kb" in tool_names
    joined = " ".join(
        m["content"] for m in main_call["messages"] if isinstance(m.get("content"), str)
    )
    assert "deadline Sep 1" in joined, "знайдене в мережі має дійти в контекст"
    assert text == "answer"


def test_web_search_step_failure_does_not_break_the_answer(monkeypatch):
    """Свіжі дані — бонус, а не умова: якщо предкрок упав, відповідь по
    архіву все одно має вийти (а не впасти у stub)."""
    import sys as _sys
    from types import SimpleNamespace

    class _Messages:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if any(t.get("name") == "web_search" for t in kwargs.get("tools", [])):
                raise RuntimeError("web down")
            return FakeResponse([text_block("archive answer")], "end_turn")

    class _Client:
        def __init__(self):
            self.messages = _Messages()

    holder = {}

    def _Anthropic(api_key=None):
        holder["client"] = _Client()
        return holder["client"]

    monkeypatch.setitem(_sys.modules, "anthropic", SimpleNamespace(Anthropic=_Anthropic))

    text, *_ = chat._llm_reply(
        [{"role": "user", "content": "verify online: anything new?"}],
        "m", "web", web_search=True,
    )
    assert text == "archive answer"
    assert len(holder["client"].messages.calls) == 2  # впав предкрок, цикл відпрацював


def test_web_search_none_result_is_not_injected(monkeypatch):
    """Порожній результат ('NONE') не має засмічувати контекст."""
    responses = [
        FakeResponse([text_block("NONE")], "end_turn"),
        FakeResponse([text_block("answer")], "end_turn"),
    ]
    holder = install_fake_anthropic(monkeypatch, responses)

    chat._llm_reply(
        [{"role": "user", "content": "check the web: xyz?"}], "m", "web", web_search=True,
    )
    main_call = holder["client"].messages.calls[1]
    assert len(main_call["messages"]) == 1, "нічого не мало додатись"


# ── Економія токенів (2026-08-11) ────────────────────────────────────


def test_rolling_cache_breakpoint_marks_only_the_latest_turn():
    """Один рухомий брейкпойнт: попередні знімаються, інакше кеш дробиться
    і впирається в ліміт API (4)."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "q"}]},
        {"role": "user", "content": [{"type": "tool_result", "content": "a"}]},
    ]
    chat._roll_cache_breakpoint(messages)
    assert messages[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}

    messages.append({"role": "user", "content": [{"type": "tool_result", "content": "b"}]})
    chat._roll_cache_breakpoint(messages)
    assert "cache_control" not in messages[1]["content"][-1], "старий має зніматись"
    assert messages[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_loop_sets_cache_breakpoint_on_tool_results(monkeypatch):
    responses = [
        FakeResponse([tool_use_block("call_1", "search_kb", {"query": "x"})], "tool_use"),
        FakeResponse([text_block("done")], "end_turn"),
    ]
    holder = install_fake_anthropic(monkeypatch, responses)
    monkeypatch.setattr(chat, "_dispatch_tool", lambda name, inp: "{}")

    chat._llm_reply([{"role": "user", "content": "q"}], "m", "web")

    second_call = holder["client"].messages.calls[1]
    last_block = second_call["messages"][-1]["content"][-1]
    assert last_block.get("cache_control") == {"type": "ephemeral"}
