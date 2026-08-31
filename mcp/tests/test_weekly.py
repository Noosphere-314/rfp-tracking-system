"""Тести weekly.py (POST /weekly-report — щотижневі звіти, 2026-08-28).

Стратегія та сама, що й у test_briefing.py: без Postgres і без мережі —
weekly._db підмінюється fakedb.make_db, anthropic — fakeanthropic.install,
kbtools.search_impl — monkeypatch'ем (він ходить у СВОЄ з'єднання повз
weekly._db, тож роутер fakedb його не бачить).

Фокус: порядок guard-ів (токен → kind → ідемпотентність → ключ), обидва
LLM-кроки (веб-провал НЕ валить звіт; провал синтезу → 502), INSERT у
kb.briefs (префікс title = ідемпотентний ключ), ігнорування кривого
model-override.
"""

from __future__ import annotations

import os
import sys

# Env — ДО імпорту weekly: DATABASE_URL читається на рівні модуля (той
# самий стиль, що й briefing.py/kbtools.py).
os.environ.setdefault("DATABASE_URL", "postgresql://x:x@127.0.0.1:1/x")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import kbtools  # noqa: E402
import weekly  # noqa: E402
from fakeanthropic import FakeResponse, install as install_fake_anthropic, text_block  # noqa: E402
from fakedb import make_db  # noqa: E402

# Guard тепер зіставляє ПОВНИЙ заголовок із сьогоднішньою датою
# (2026-08-31: вікно «3 доби» глушило понеділковий крон через п'ятничний
# ручний прогін) — тож фікстури мусять нести саме сьогоднішній день.
from datetime import date  # noqa: E402

TODAY = date.today().isoformat()


def _router(recent=None, settings=None, topics=None, forums=None, inserted_id=77):
    """Роутер fakedb під запити weekly.py. settings — dict key→value."""
    settings = settings or {}

    def settings_rows(params):
        key = params[0]
        return [{"value": settings[key]}] if key in settings else []

    return [
        ("WHERE title = ", recent or []),
        ("SELECT value FROM settings", settings_rows),
        ("bumped_at > now()", topics or []),
        ("FROM kb.forums", forums or []),
        ("INSERT INTO kb.briefs", [{"id": inserted_id}]),
    ]


def _wire(monkeypatch, router, responses, api_key="test-key", token="tok"):
    """Стандартний монтаж: env, _db, search_impl, anthropic. Повертає
    (holder, calls) — client.messages.calls і список SQL-викликів."""
    if token:
        monkeypatch.setenv("KB_MCP_TOKEN", token)
    else:
        monkeypatch.delenv("KB_MCP_TOKEN", raising=False)
    monkeypatch.setattr(weekly, "ANTHROPIC_API_KEY", api_key)
    db_factory, calls = make_db(router)
    monkeypatch.setattr(weekly, "_db", db_factory)
    monkeypatch.setattr(kbtools, "search_impl",
                        lambda query, limit=8, **kw: {"hits": [], "topic_hits": []})
    holder = install_fake_anthropic(monkeypatch, responses)
    return holder, calls


# ── guard-и, по порядку ─────────────────────────────────────────────


def test_missing_token_fails_closed(monkeypatch):
    _wire(monkeypatch, _router(), [], token=None)
    result, status = weekly.generate({"kind": "grants"})
    assert status == 403
    assert not result["ok"]


def test_unknown_kind_is_422(monkeypatch):
    _wire(monkeypatch, _router(), [])
    result, status = weekly.generate({"kind": "monthly"})
    assert status == 422
    assert "kind" in result["error"]


def test_recent_report_short_circuits_without_llm(monkeypatch):
    # responses=[] — будь-який виклик fake-клієнта впав би AssertionError,
    # тож зелений тест і є доказом, що LLM не чіпали.
    _wire(monkeypatch,
          _router(recent=[{"id": 55, "title": f"Weekly grants & RFPs — {TODAY}",
                           "brief_md": "## New this week\n- Something"}]),
          [])
    result, status = weekly.generate({"kind": "grants"})
    assert status == 200
    assert result["skipped"] is True
    assert result["brief_id"] == 55
    # Повторний прогін теж несе суть у Telegram, а не саме посилання.
    assert "New this week" in result["summary"]


def test_force_bypasses_the_recent_guard(monkeypatch):
    holder, _ = _wire(
        monkeypatch,
        _router(recent=[{"id": 55, "title": f"Weekly grants & RFPs — {TODAY}",
                           "brief_md": "## New this week\n- Something"}]),
        [FakeResponse([text_block("web findings")], "end_turn"),
         FakeResponse([text_block("## report")], "end_turn")],
    )
    result, status = weekly.generate({"kind": "grants", "force": True})
    assert status == 200
    assert result["skipped"] is False


def test_missing_api_key_is_503_no_stub_tier(monkeypatch):
    _wire(monkeypatch, _router(), [], api_key="")
    result, status = weekly.generate({"kind": "grants"})
    assert status == 503
    assert not result["ok"]


# ── happy path і деградації ─────────────────────────────────────────


def test_happy_path_inserts_brief_and_returns_summary(monkeypatch):
    holder, calls = _wire(
        monkeypatch,
        _router(settings={"brief_model": "claude-opus-5", "brief_language": "uk"},
                topics=[{"title": "RFP: audits", "forum_slug": "optimism",
                         "url": "https://gov.optimism.io/t/1"}]),
        [FakeResponse([text_block("Fresh grants found: X")], "end_turn",
                      tokens_in=100, tokens_out=50),
         FakeResponse([text_block(
             "## New this week\n- X grant\n---TELEGRAM---\n"
             "- X grant, Optimism, closes Sep 5\nNext: draft the bid")],
             "end_turn", tokens_in=200, tokens_out=80)],
    )
    result, status = weekly.generate({"kind": "grants"})
    assert status == 200
    assert result["ok"] and result["skipped"] is False
    assert result["brief_id"] == 77
    assert result["title"].startswith("Weekly grants & RFPs — ")
    # summary — ДАЙДЖЕСТ із-за маркера, не зріз звіту.
    assert result["summary"].startswith("- X grant, Optimism")
    assert "Next: draft the bid" in result["summary"]

    insert = next((sql, p) for sql, p in calls if "INSERT INTO kb.briefs" in sql)
    # У kb.briefs — сам звіт, БЕЗ маркера й дайджесту.
    assert "---TELEGRAM---" not in insert[1][1]
    assert "Next: draft the bid" not in insert[1][1]
    assert insert[1][0].startswith("Weekly grants & RFPs — ")   # title-префікс
    assert insert[1][2] == "claude-opus-5"
    assert insert[1][3] == 300 and insert[1][4] == 130          # суми токенів

    web_call, syn_call = holder["client"].messages.calls
    assert web_call["tools"][0]["type"] == "web_search_20260209"
    assert "tools" not in syn_call
    # Мова звіту — з settings.brief_language.
    assert "Ukrainian" in syn_call["system"][0]["text"]
    # Правило формату (md_lite не знає "# ") доклеєне до кожного system.
    assert "only understands" in syn_call["system"][0]["text"]


def test_web_failure_still_produces_a_report(monkeypatch):
    holder, calls = _wire(
        monkeypatch,
        _router(settings={"brief_model": "claude-opus-5"}),
        [RuntimeError("web search down"),
         FakeResponse([text_block("## report without web")], "end_turn")],
    )
    result, status = weekly.generate({"kind": "discovery"})
    assert status == 200
    assert result["ok"]
    # Синтез отримав чесну позначку замість вигаданих findings.
    syn_call = holder["client"].messages.calls[-1]
    assert "web research unavailable" in syn_call["messages"][0]["content"]


def test_synthesis_failure_is_502(monkeypatch):
    _wire(
        monkeypatch,
        _router(settings={"brief_model": "claude-opus-5"}),
        [FakeResponse([text_block("web ok")], "end_turn"),
         RuntimeError("api down")],
    )
    result, status = weekly.generate({"kind": "grants"})
    assert status == 502
    assert not result["ok"]


def test_invalid_model_override_falls_back_to_setting(monkeypatch):
    holder, _ = _wire(
        monkeypatch,
        _router(settings={"brief_model": "claude-opus-5"}),
        [FakeResponse([text_block("web")], "end_turn"),
         FakeResponse([text_block("## r")], "end_turn")],
    )
    result, status = weekly.generate({"kind": "grants", "model": "gpt-6; DROP TABLE"})
    assert status == 200
    assert holder["client"].messages.calls[0]["model"] == "claude-opus-5"


def test_valid_model_override_wins(monkeypatch):
    holder, _ = _wire(
        monkeypatch,
        _router(settings={"brief_model": "claude-opus-5"}),
        [FakeResponse([text_block("web")], "end_turn"),
         FakeResponse([text_block("## r")], "end_turn")],
    )
    weekly.generate({"kind": "grants", "model": "claude-sonnet-5"})
    assert holder["client"].messages.calls[0]["model"] == "claude-sonnet-5"


def test_discovery_context_includes_tracked_forums(monkeypatch):
    holder, _ = _wire(
        monkeypatch,
        _router(settings={"brief_model": "claude-opus-5"},
                forums=[{"forum_slug": "optimism",
                         "base_url": "https://gov.optimism.io"}]),
        [FakeResponse([text_block("web")], "end_turn"),
         FakeResponse([text_block("## r")], "end_turn")],
    )
    weekly.generate({"kind": "discovery"})
    syn_call = holder["client"].messages.calls[-1]
    assert "optimism (https://gov.optimism.io)" in syn_call["messages"][0]["content"]


def test_discovery_context_carries_the_denylist(monkeypatch):
    """Свідомо викинуті форуми (Lido) не з'являються у списку відстежуваних
    САМЕ ТОМУ, що їх видалили — без окремого рядка звіт пропонував би їх
    щотижня. Перший живий звіт 2026-08-28 так і зробив."""
    holder, _ = _wire(
        monkeypatch,
        _router(settings={"brief_model": "claude-opus-5",
                          "weekly_forum_denylist": "lido, foo"}),
        [FakeResponse([text_block("web")], "end_turn"),
         FakeResponse([text_block("## r")], "end_turn")],
    )
    weekly.generate({"kind": "discovery"})
    context = holder["client"].messages.calls[-1]["messages"][0]["content"]
    assert "Deliberately rejected" in context
    assert "lido, foo" in context


def test_grants_context_has_no_forum_lists(monkeypatch):
    """Списки форумів — суто discovery-секція «Forums worth adding»; у
    grants вони були б витраченими токенами."""
    holder, _ = _wire(
        monkeypatch,
        _router(settings={"brief_model": "claude-opus-5"}),
        [FakeResponse([text_block("web")], "end_turn"),
         FakeResponse([text_block("## r")], "end_turn")],
    )
    weekly.generate({"kind": "grants"})
    context = holder["client"].messages.calls[-1]["messages"][0]["content"]
    assert "Deliberately rejected" not in context
    assert "Tracked forums" not in context


# ── дайджест для Telegram ───────────────────────────────────────────


def test_plain_text_strips_markdown_for_the_messenger():
    """Правило Миколи: у Telegram жодних ** і # — лише чистий текст, а
    посилання голим URL (він там клікабельний сам)."""
    out = weekly._plain_text(
        "## Head\n**bold** and *italic* and `code`\n"
        "- [ENS RFP](https://discuss.ens.domains/t/1)\n---\n"
    )
    assert "#" not in out and "**" not in out and "`" not in out
    assert "Head" in out and "bold" in out and "italic" in out
    assert "ENS RFP — https://discuss.ens.domains/t/1" in out


def test_plain_text_keeps_underscores_in_urls():
    """Курсив підкресленнями НЕ чіпаємо: підкреслення живуть у URL-ах."""
    out = weekly._plain_text("see https://example.com/a_b_c now")
    assert "a_b_c" in out


def test_fallback_digest_prefers_bullets_over_the_intro_caveat():
    """Перша жива перевірка 2026-08-28 віддала в Telegram вступне
    «тиждень тонкий» замість конкретики — резерв тепер бере пункти."""
    report = (
        "## New this week\n"
        "Honest framing: this is a thin week, coverage is incomplete.\n"
        "- **ENS SPP3 Marketplace RFP** — ENS DAO. Budget undisclosed, and "
        "the thread may be a shortlist rather than an open call.\n"
        "- **Arbitrum Security Program** — Arbitrum DAO. Size unverified, "
        "live as a Snapshot proposal this week.\n"
    )
    digest = weekly._fallback_digest(report)
    assert digest.startswith("- ENS SPP3 Marketplace RFP — ENS DAO.")
    assert "Arbitrum Security Program" in digest
    assert "Honest framing" not in digest
    # Обрізано по межі речення, а не посеред слова.
    assert "Budget undisclosed" not in digest


def test_fallback_digest_cuts_on_line_boundaries():
    """Без маркера дайджест ріжеться ПО РЯДКАХ — механічний зріз на N
    символів рвав речення посеред слова (та сама причина, чому summary
    більше не report[:400])."""
    report = "## Head\n" + "\n".join(
        f"Paragraph number {i} without any bullet marker." for i in range(60))
    digest = weekly._fallback_digest(report, limit=100)
    assert len(digest) <= 120
    assert digest.split("\n")[-1].endswith(".")


def test_synthesis_without_the_marker_falls_back(monkeypatch):
    holder, calls = _wire(
        monkeypatch,
        _router(settings={"brief_model": "claude-opus-5"}),
        [FakeResponse([text_block("web")], "end_turn"),
         FakeResponse([text_block("## New this week\n- Only the report")],
                      "end_turn")],
    )
    result, status = weekly.generate({"kind": "grants"})
    assert status == 200
    assert "Only the report" in result["summary"]


def test_telegram_rule_reaches_the_prompt(monkeypatch):
    holder, _ = _wire(
        monkeypatch,
        _router(settings={"brief_model": "claude-opus-5"}),
        [FakeResponse([text_block("web")], "end_turn"),
         FakeResponse([text_block("## r\n---TELEGRAM---\n- x")], "end_turn")],
    )
    weekly.generate({"kind": "grants"})
    system = holder["client"].messages.calls[-1]["system"][0]["text"]
    assert "---TELEGRAM---" in system
    assert "600 characters" in system


def test_guard_ignores_a_report_from_an_earlier_day(monkeypatch):
    """Регресія 2026-08-31: понеділковий крон НЕ згенерував нічого, бо
    п'ятничні звіти потрапляли у вікно «3 доби», і команда отримала в
    Telegram п'ятничні звіти під виглядом понеділкових. Ключ — точний
    заголовок за сьогодні, тож учорашній звіт більше не глушить прогін."""
    db_factory, calls = make_db([
        # Реальний SQL тепер фільтрує за title = сьогоднішній; фейк віддає
        # порожньо саме тому, що збігу за сьогодні немає.
        ("WHERE title = ", []),
        ("SELECT value FROM settings", lambda p: [{"value": "claude-opus-5"}]
         if p[0] == "brief_model" else []),
        ("INSERT INTO kb.briefs", [{"id": 91}]),
    ])
    monkeypatch.setenv("KB_MCP_TOKEN", "tok")
    monkeypatch.setattr(weekly, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(weekly, "_db", db_factory)
    monkeypatch.setattr(kbtools, "search_impl",
                        lambda query, limit=8, **kw: {"hits": []})
    install_fake_anthropic(monkeypatch, [
        FakeResponse([text_block("web")], "end_turn"),
        FakeResponse([text_block("## r\n- item one. detail\n- item two. detail")],
                     "end_turn"),
    ])

    result, status = weekly.generate({"kind": "grants"})
    assert status == 200 and result["skipped"] is False
    assert result["brief_id"] == 91
    # Guard шукав саме сьогоднішній заголовок.
    guard = next(p for sql, p in calls if "WHERE title = " in sql)
    assert guard[0] == f"Weekly grants & RFPs — {TODAY}"


def test_guard_and_title_share_one_date(monkeypatch):
    """Дата рахується один раз: інакше прогін через опівніч перевірив би
    вчорашній ключ, а записав сьогоднішній — і зробив би два звіти."""
    holder, calls = _wire(
        monkeypatch,
        _router(settings={"brief_model": "claude-opus-5"}),
        [FakeResponse([text_block("web")], "end_turn"),
         FakeResponse([text_block("## r\n- a. b\n- c. d")], "end_turn")],
    )
    weekly.generate({"kind": "discovery"})
    guard = next(p for sql, p in calls if "WHERE title = " in sql)
    insert = next(p for sql, p in calls if "INSERT INTO kb.briefs" in sql)
    assert guard[0] == insert[0]
