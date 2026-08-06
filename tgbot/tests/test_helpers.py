"""Тести чистих функцій tgbot/main.py — без мережі, без Telegram, без kbmcp.

Покриває саме те, що легко зламати непомітно: підрахунок довжини по
UTF-16 code units (а не python-символах — розходиться на емодзі), межі
розрізу шматків повідомлення, парсинг allowlist з .env і ротацію сесії.
"""

from __future__ import annotations

from tgbot.main import (
    chunk_message,
    md_links_to_plain,
    parse_allowlist,
    rotate_session,
    session_key,
)

# ── md_links_to_plain ────────────────────────────────────────────────


def test_md_links_to_plain_rewrites_multiple_links():
    text = (
        "See [OP Docs](https://docs.optimism.io) and "
        "[Arbitrum](https://arbitrum.io) for details."
    )
    assert md_links_to_plain(text) == (
        "See OP Docs: https://docs.optimism.io and "
        "Arbitrum: https://arbitrum.io for details."
    )


def test_md_links_to_plain_leaves_text_without_links_unchanged():
    text = "Just plain text, no links here. Nothing to rewrite."
    assert md_links_to_plain(text) == text


def test_md_links_to_plain_nested_brackets_are_left_as_is():
    # Мітка з вкладеними дужками "[a [b] c]" — не валідний одинарний
    # markdown-лінк за нашим спрощеним парсером; безпечніше лишити сирий
    # текст, ніж скалічити його неправильним розбором.
    text = "Check [a [b] c](http://example.com) out."
    assert md_links_to_plain(text) == text


def test_md_links_to_plain_handles_empty_string():
    assert md_links_to_plain("") == ""


# ── chunk_message ─────────────────────────────────────────────────────


def test_chunk_message_empty_string_returns_no_chunks():
    assert chunk_message("") == []


def test_chunk_message_short_text_is_a_single_chunk():
    text = "hello there"
    assert chunk_message(text, limit=3500) == [text]


def test_chunk_message_counts_utf16_code_units_not_python_chars():
    # 😀 (U+1F600) — астральний символ: 1 python-символ, 2 UTF-16 code units.
    # 3000 таких символів — це 3000 python-символів (< наївного ліміту 3500),
    # але 6000 code units (> 3500). Наївне розбиття по len(s) дало б ОДИН
    # шматок і зламало б sendMessage на реальному Telegram-ліміті.
    emoji_text = "\U0001F600" * 3000
    limit = 3500

    chunks = chunk_message(emoji_text, limit=limit)

    assert len(chunks) > 1, "мало розбити на кілька шматків через code units, а не символи"
    for chunk in chunks:
        utf16_units = len(chunk.encode("utf-16-le")) // 2
        assert utf16_units <= limit
    # Жоден символ не загублено і не задубльовано (немає переносів рядків,
    # які поглинаються на межі розрізу).
    assert "".join(chunks) == emoji_text


def test_chunk_message_prefers_paragraph_boundary():
    para_a = "A" * 30
    para_b = "B" * 30
    text = f"{para_a}\n\n{para_b}"
    chunks = chunk_message(text, limit=50)
    assert chunks == [para_a, para_b]


def test_chunk_message_falls_back_to_single_newline():
    line_a = "A" * 30
    line_b = "B" * 30
    text = f"{line_a}\n{line_b}"
    chunks = chunk_message(text, limit=50)
    assert chunks == [line_a, line_b]


def test_chunk_message_hard_splits_when_no_boundary_exists():
    text = "X" * 100
    chunks = chunk_message(text, limit=40)
    assert len(chunks) == 3
    for chunk in chunks:
        assert len(chunk.encode("utf-16-le")) // 2 <= 40
    assert "".join(chunks) == text


def test_chunk_message_avoids_splitting_a_source_line_when_a_newline_boundary_exists():
    # "label: url" — форма рядка джерела після md_links_to_plain. Якщо в тексті
    # є перенос рядка перед ним, розріз має статись ТАМ, а не всередині рядка.
    intro = "x" * 30
    source_line = "sources: https://example.com/path/to/thread/12345"
    text = f"{intro}\n{source_line}"
    # Ліміт обраний так, щоб наївний "жорсткий" розріз по символах впав би
    # рівно посередині source_line.
    limit = len(intro) + len(source_line) // 2

    chunks = chunk_message(text, limit=limit)

    assert chunks == [intro, source_line], chunks


# ── parse_allowlist ───────────────────────────────────────────────────


def test_parse_allowlist_tolerates_spaces_and_empty_segments():
    assert parse_allowlist("1, 2,,3 ") == {1, 2, 3}


def test_parse_allowlist_empty_string_is_empty_set():
    assert parse_allowlist("") == set()


def test_parse_allowlist_skips_non_numeric_segments():
    assert parse_allowlist("42, not-a-number, 7") == {42, 7}


def test_parse_allowlist_handles_negative_group_ids():
    # Не використовується для allowlist у проді (то user id, завжди додатні),
    # але функція не має мовчки ламатись на "-" — int() його приймає штатно.
    assert parse_allowlist("-100123, 5") == {-100123, 5}


# ── session_key / rotate_session ────────────────────────────────────


def test_session_key_before_any_rotation_is_the_bare_chat_id():
    assert session_key(12345, {}) == "12345"


def test_session_key_bare_chat_id_has_no_colon_even_for_negative_group_ids():
    # Групові chat_id від'ємні ("-100..."), знак мінус — не двокрапка.
    key = session_key(-1001234567890, {})
    assert key == "-1001234567890"
    assert ":" not in key


def test_rotate_session_produces_chatid_dash_timestamp_with_no_colon():
    rotations: dict[int, int] = {}
    key = rotate_session(555, rotations, now=1700000000)
    assert key == "555-1700000000"
    assert ":" not in key
    # Наступний виклик session_key бачить зафіксовану ротацію.
    assert session_key(555, rotations) == key


def test_rotate_session_twice_changes_the_suffix():
    rotations: dict[int, int] = {}
    first = rotate_session(555, rotations, now=100)
    second = rotate_session(555, rotations, now=200)
    assert first == "555-100"
    assert second == "555-200"
    assert first != second


def test_rotate_session_does_not_affect_other_chats():
    rotations: dict[int, int] = {}
    rotate_session(1, rotations, now=100)
    assert session_key(2, rotations) == "2"
