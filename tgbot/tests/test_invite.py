"""Тести інвайт-based онбордингу: allowlist.json (стан + I/O) і чисті команди
/invite, /start <code>, /who, /revoke.

Той самий принцип, що й test_helpers.py: жодного реального Telegram/kbmcp —
лише чисті функції та файловий I/O у tmp_path (pytest fixture).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from tgbot.main import (
    ADMIN_ONLY_TEXT,
    INVITE_INVALID_TEXT,
    INVITE_ONLY_TEXT,
    INVITE_WELCOME_TEXT,
    REVOKE_USAGE_TEXT,
    START_TEXT,
    AllowlistState,
    create_invite,
    format_who,
    handle_invite_command,
    handle_revoke_command,
    handle_start_command,
    handle_who_command,
    is_invited,
    load_state,
    parse_start_payload,
    redeem_invite,
    revoke_invited,
    save_state,
)

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)


# ── parse_start_payload (deep-link payload) ─────────────────────────────


def test_parse_start_payload_with_code():
    assert parse_start_payload("/start abc123") == "abc123"


def test_parse_start_payload_bare_start_is_none():
    assert parse_start_payload("/start") is None


def test_parse_start_payload_ignores_trailing_tokens():
    assert parse_start_payload("/start abc123 extra stuff") == "abc123"


# ── create_invite / redeem_invite ────────────────────────────────────────


def test_create_invite_stores_record_with_injected_clock():
    state = AllowlistState()
    code = create_invite(state, admin_id=1, now=NOW, code="fixed-code")
    assert code == "fixed-code"
    assert state.invites["fixed-code"] == {
        "created_by": 1,
        "created_at": NOW.isoformat(),
        "used_by": None,
    }


def test_create_invite_generates_a_code_when_none_given():
    state = AllowlistState()
    code = create_invite(state, admin_id=1, now=NOW)
    assert code in state.invites
    assert len(code) > 10  # secrets.token_urlsafe(12) — не пусто, не тривіально коротко


def test_redeem_invite_happy_path_marks_used_and_adds_invited():
    state = AllowlistState()
    create_invite(state, admin_id=1, now=NOW, code="good-code")

    result = redeem_invite(state, "good-code", user_id=42, now=NOW + timedelta(minutes=5))

    assert result == "ok"
    assert state.invites["good-code"]["used_by"] == 42
    assert is_invited(state, 42) is True
    assert state.invited["42"]["via"] == "good-cod"  # code[:8]


def test_redeem_invite_garbage_code_is_invalid():
    state = AllowlistState()
    assert redeem_invite(state, "no-such-code", user_id=42, now=NOW) == "invalid"
    assert is_invited(state, 42) is False


def test_redeem_invite_already_used_is_used():
    state = AllowlistState()
    create_invite(state, admin_id=1, now=NOW, code="one-shot")
    first = redeem_invite(state, "one-shot", user_id=42, now=NOW)
    second = redeem_invite(state, "one-shot", user_id=99, now=NOW)

    assert first == "ok"
    assert second == "used"
    assert is_invited(state, 99) is False  # другий претендент НЕ пройшов


def test_redeem_invite_past_ttl_is_expired():
    state = AllowlistState()
    create_invite(state, admin_id=1, now=NOW, code="stale-code")

    result = redeem_invite(state, "stale-code", user_id=42, now=NOW + timedelta(days=8))

    assert result == "expired"
    assert is_invited(state, 42) is False


def test_redeem_invite_just_under_ttl_still_ok():
    state = AllowlistState()
    create_invite(state, admin_id=1, now=NOW, code="fresh-code")

    result = redeem_invite(state, "fresh-code", user_id=42, now=NOW + timedelta(days=6, hours=23))

    assert result == "ok"


# ── revoke_invited ────────────────────────────────────────────────────


def test_revoke_invited_removes_user():
    state = AllowlistState(invited={"42": {"added_at": "x", "via": "y"}})
    assert revoke_invited(state, admin_ids={1}, user_id=42) == "ok"
    assert is_invited(state, 42) is False


def test_revoke_invited_cannot_revoke_admin():
    state = AllowlistState(invited={"1": {"added_at": "x", "via": "y"}})
    assert revoke_invited(state, admin_ids={1}, user_id=1) == "is-admin"
    # Адміна не зняли навіть якщо він якимось чином опинився в invited.
    assert "1" in state.invited


def test_revoke_invited_unknown_user_is_not_invited():
    state = AllowlistState()
    assert revoke_invited(state, admin_ids={1}, user_id=999) == "not-invited"


# ── format_who ────────────────────────────────────────────────────────


def test_format_who_lists_admins_and_invited():
    state = AllowlistState(invited={"42": {"added_at": "2026-01-01T00:00:00+00:00", "via": "abc"}})
    text = format_who({1, 2}, state)
    assert "Admins:" in text
    assert "  1" in text
    assert "  2" in text
    assert "Invited:" in text
    assert "42" in text
    assert "2026-01-01T00:00:00+00:00" in text


def test_format_who_handles_empty_invited():
    text = format_who({1}, AllowlistState())
    assert "(none)" in text


# ── persistence roundtrip (load_state / save_state) ─────────────────────


def test_save_then_load_roundtrips(tmp_path):
    state = AllowlistState(
        invited={"42": {"added_at": NOW.isoformat(), "via": "abcdefgh"}},
        invites={"code1": {"created_by": 1, "created_at": NOW.isoformat(), "used_by": 42}},
    )
    save_state(str(tmp_path), state)

    loaded = load_state(str(tmp_path))

    assert loaded.invited == state.invited
    assert loaded.invites == state.invites


def test_load_state_missing_file_returns_empty(tmp_path):
    loaded = load_state(str(tmp_path / "does-not-exist"))
    assert loaded.invited == {}
    assert loaded.invites == {}


def test_load_state_corrupt_json_returns_empty_without_raising(tmp_path):
    (tmp_path / "allowlist.json").write_text("{ not valid json", encoding="utf-8")

    loaded = load_state(str(tmp_path))

    assert loaded.invited == {}
    assert loaded.invites == {}


def test_load_state_unexpected_shape_returns_empty(tmp_path):
    (tmp_path / "allowlist.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    loaded = load_state(str(tmp_path))

    assert loaded.invited == {}
    assert loaded.invites == {}


def test_save_state_is_atomic_no_leftover_tmp_files(tmp_path):
    save_state(str(tmp_path), AllowlistState())
    leftovers = list(tmp_path.glob(".allowlist-*.tmp"))
    assert leftovers == []
    assert (tmp_path / "allowlist.json").exists()


# ── handle_invite_command ────────────────────────────────────────────


def test_handle_invite_command_admin_gets_a_link():
    state = AllowlistState()
    reply, changed = handle_invite_command(state, {1}, 1, "rfp_kb_bot", now=NOW, code="abc")
    assert changed is True
    assert "https://t.me/rfp_kb_bot?start=abc" in reply
    assert "abc" in state.invites


def test_handle_invite_command_non_admin_is_refused():
    state = AllowlistState()
    reply, changed = handle_invite_command(state, {1}, 2, "rfp_kb_bot", now=NOW, code="abc")
    assert reply == ADMIN_ONLY_TEXT
    assert changed is False
    assert state.invites == {}


def test_handle_invite_command_falls_back_without_bot_username():
    state = AllowlistState()
    reply, changed = handle_invite_command(state, {1}, 1, None, now=NOW, code="abc")
    assert changed is True
    assert "abc" in reply
    assert "https://t.me" not in reply


# ── handle_start_command ─────────────────────────────────────────────


def test_handle_start_command_already_allowed_no_payload_gets_greeting():
    state = AllowlistState()
    reply, changed = handle_start_command(state, {1}, 1, "/start", now=NOW)
    assert reply == START_TEXT
    assert changed is False


def test_handle_start_command_stranger_no_payload_gets_invite_only_text():
    state = AllowlistState()
    reply, changed = handle_start_command(state, {1}, 999, "/start", now=NOW)
    assert reply == INVITE_ONLY_TEXT
    assert changed is False


def test_handle_start_command_valid_code_admits_and_welcomes():
    state = AllowlistState()
    create_invite(state, admin_id=1, now=NOW, code="good")

    reply, changed = handle_start_command(state, {1}, 999, "/start good", now=NOW)

    assert reply == INVITE_WELCOME_TEXT
    assert changed is True
    assert is_invited(state, 999) is True


def test_handle_start_command_already_allowed_with_payload_just_greets():
    state = AllowlistState()
    create_invite(state, admin_id=1, now=NOW, code="good")

    # Адмін переходить за чиїмось інвайт-лінком (наприклад, тестує її) — код
    # не витрачається, звичайне вітання.
    reply, changed = handle_start_command(state, {1}, 1, "/start good", now=NOW)

    assert reply == START_TEXT
    assert changed is False
    assert state.invites["good"]["used_by"] is None


def test_handle_start_command_garbage_used_and_expired_all_give_identical_generic_refusal():
    # Навмисно: жодна з трьох причин відмови не повинна витікати назовні
    # по-різному — це і є "не розкривати семантику коду".
    state = AllowlistState()
    create_invite(state, admin_id=1, now=NOW, code="used-up")
    redeem_invite(state, "used-up", user_id=1, now=NOW)
    create_invite(state, admin_id=1, now=NOW, code="stale")

    garbage_reply, _ = handle_start_command(state, {1}, 100, "/start nonsense", now=NOW)
    used_reply, _ = handle_start_command(state, {1}, 101, "/start used-up", now=NOW)
    expired_reply, _ = handle_start_command(state, {1}, 102, "/start stale", now=NOW + timedelta(days=30))

    assert garbage_reply == used_reply == expired_reply == INVITE_INVALID_TEXT
    assert is_invited(state, 100) is False
    assert is_invited(state, 101) is False
    assert is_invited(state, 102) is False


# ── handle_who_command ────────────────────────────────────────────────


def test_handle_who_command_admin_only():
    state = AllowlistState()
    reply, changed = handle_who_command(state, {1}, 2)
    assert reply == ADMIN_ONLY_TEXT
    assert changed is False


def test_handle_who_command_admin_sees_listing():
    state = AllowlistState(invited={"42": {"added_at": NOW.isoformat(), "via": "abc"}})
    reply, changed = handle_who_command(state, {1}, 1)
    assert changed is False
    assert "42" in reply
    assert "1" in reply


# ── handle_revoke_command ────────────────────────────────────────────


def test_handle_revoke_command_non_admin_is_refused():
    state = AllowlistState(invited={"42": {"added_at": NOW.isoformat(), "via": "abc"}})
    reply, changed = handle_revoke_command(state, {1}, 2, " 42")
    assert reply == ADMIN_ONLY_TEXT
    assert changed is False
    assert is_invited(state, 42) is True


def test_handle_revoke_command_admin_revokes_invited_user():
    state = AllowlistState(invited={"42": {"added_at": NOW.isoformat(), "via": "abc"}})
    reply, changed = handle_revoke_command(state, {1}, 1, " 42")
    assert changed is True
    assert "42" in reply
    assert is_invited(state, 42) is False


def test_handle_revoke_command_cannot_revoke_admin():
    state = AllowlistState()
    reply, changed = handle_revoke_command(state, {1, 2}, 1, " 2")
    assert changed is False
    assert "admin" in reply.lower()


def test_handle_revoke_command_unknown_user():
    state = AllowlistState()
    reply, changed = handle_revoke_command(state, {1}, 1, " 999")
    assert changed is False
    assert "999" in reply


def test_handle_revoke_command_missing_argument_shows_usage():
    state = AllowlistState()
    reply, changed = handle_revoke_command(state, {1}, 1, "")
    assert reply == REVOKE_USAGE_TEXT
    assert changed is False


def test_handle_revoke_command_non_numeric_argument_shows_usage():
    state = AllowlistState()
    reply, changed = handle_revoke_command(state, {1}, 1, " not-a-number")
    assert reply == REVOKE_USAGE_TEXT
    assert changed is False
