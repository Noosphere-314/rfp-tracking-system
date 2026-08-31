"""Два різні сигнали про мовчазні джерела: «мертве» і «тихе».

31.08.2026 алерт «8 source(s) have produced nothing for 14+ days» прилетів
у бот двічі за годину. Розбір дав два окремі баги.

1. Дедуп не спрацював, бо порівнює message ПОБУКВЕНО, а `ORDER BY
   last_item_at NULLS FIRST` не визначає порядок серед джерел із порожнім
   last_item_at — Postgres віддавав ті самі вісім щоразу інакше.
2. Самі вісім не були аварією: всі ендпойнти віддавали 200 і реальний
   контент, просто в тихій grants-категорії найновіша тема буває
   піврічної давнини. Щогодинний алерт про це — шум, від якого бота
   вимикають разом зі справжніми аваріями.
"""

from __future__ import annotations

from datetime import datetime, timezone

from worker.pipeline import (
    DARK_LISTED,
    QUIET_PREFIX,
    _dead_message,
    _quiet_message,
    _source_lines,
)


def _row(name: str, ecosystem: str = "multi", last_item_at=None) -> dict:
    return {"name": name, "ecosystem": ecosystem, "last_item_at": last_item_at}


def _at(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=timezone.utc)


def test_order_of_rows_does_not_change_the_message():
    """Ключ дедупу — готовий рядок, тож він має бути незалежним від БД."""
    rows = [_row("Safe forum"), _row("dYdX forum"), _row("Polygon forum")]

    assert _dead_message(rows) == _dead_message(list(reversed(rows)))
    assert _dead_message(rows) == _dead_message([rows[1], rows[2], rows[0]])


def test_never_produced_sources_come_before_merely_stale_ones():
    lines = _source_lines([_row("Stale", last_item_at=_at(7)), _row("Never")])

    assert lines == ["• Never (multi)", "• Stale (multi)"]


def test_sources_with_timestamps_are_listed_oldest_first():
    lines = _source_lines(
        [_row("Newer", last_item_at=_at(9)), _row("Older", last_item_at=_at(2))]
    )

    assert lines == ["• Older (multi)", "• Newer (multi)"]


def test_long_lists_say_how_many_were_not_shown():
    rows = [_row(f"Source {i:02d}") for i in range(DARK_LISTED + 3)]

    lines = _source_lines(rows)

    assert len(lines) == DARK_LISTED + 1
    assert lines[-1] == "…and 3 more"


def test_short_lists_have_no_truncation_note():
    assert _source_lines([_row("Only one")]) == ["• Only one (multi)"]


def test_the_two_signals_read_differently():
    """Мертве кличе лізти й дивитись; тихе — лише повідомляє."""
    rows = [_row("Safe forum", "Safe", last_item_at=_at(3))]

    dead, quiet = _dead_message(rows), _quiet_message(rows, 14)

    assert "NOTHING on a full-history sweep" in dead
    assert "moved" in dead
    assert "still fetch fine" in quiet
    assert "moved" not in quiet


def test_the_quiet_digest_is_findable_by_its_prefix():
    """_quiet_digest_due шукає в alerts саме за цим префіксом."""
    assert _quiet_message([_row("Safe forum")], 14).startswith(QUIET_PREFIX)
