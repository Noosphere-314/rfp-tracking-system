"""Водяний знак: мітка з майбутнього не має замикати джерело.

DAOstar мапить ts на closeDate — це ДЕДЛАЙН пулу, тобто дата попереду.
Один пул із 2028-09-30 підняв last_item_at у 2028 рік, після чого кожен
реальний пул відсікався як «застарий» (rest_aggregator: `if ts < since:
continue`). Джерело мовчало місяцями, і алерт про темні джерела його не
бачив: він шукає last_item_at у МИНУЛОМУ. Знайдено 31.08.2026 під час
розбору алерта про вісім мовчазних джерел.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from worker.pipeline import SEED_LOOKBACK, _watermark, _watermark_marker

NOW = datetime(2026, 8, 31, 18, 0, tzinfo=timezone.utc)


def _item(ts=None, watermark_ts=None):
    """Лише ті два поля, які читає _watermark_marker."""
    return type("_Item", (), {"ts": ts, "watermark_ts": watermark_ts})()


def test_a_timestamp_from_the_future_is_not_trusted():
    assert _watermark_marker(_item(ts=NOW + timedelta(days=740)), NOW) is None


def test_a_normal_timestamp_passes_through():
    ts = NOW - timedelta(days=3)
    assert _watermark_marker(_item(ts=ts), NOW) == ts


def test_the_present_moment_is_still_trusted():
    assert _watermark_marker(_item(ts=NOW), NOW) == NOW


def test_watermark_ts_wins_over_ts():
    ts, wm = NOW - timedelta(days=9), NOW - timedelta(days=1)
    assert _watermark_marker(_item(ts=ts, watermark_ts=wm), NOW) == wm


def test_a_future_watermark_ts_is_dropped_even_when_ts_is_sane():
    item = _item(ts=NOW - timedelta(days=2), watermark_ts=NOW + timedelta(days=400))
    assert _watermark_marker(item, NOW) is None


def test_an_item_without_any_timestamp_yields_nothing():
    assert _watermark_marker(_item(), NOW) is None


def test_seed_mode_reaches_back_past_any_real_forum_history():
    reach = datetime.now(timezone.utc) - _watermark({"last_item_at": NOW}, True)
    assert reach >= SEED_LOOKBACK - timedelta(seconds=5)


def test_without_history_the_window_rides_last_success_at():
    """Причина, з якої добір існує: вікно тут вузьке, і саме воно тримало
    п'ять джерел на нулі — last_success_at оновлюється щопрогону."""
    last_success = NOW - timedelta(minutes=30)
    row = {"last_item_at": None, "last_success_at": last_success}

    assert _watermark(row, False) > NOW - timedelta(hours=2)
