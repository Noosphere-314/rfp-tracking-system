"""Валідація рядка `sources` на читанні (інваріант A4, worker/fetchers/base.py).

Окремий файл з'явився через живий баг 2026-08-12: форма «Додати джерело» в
адмінці падала 500-кою на КОЖНОМУ джерелі, бо Source.from_row перевіряла
обов'язкові поля на truthiness, а admin передає ще не збереженого кандидата
з id=0 (рядка в БД ще нема — тест-фетч іде ДО INSERT). Жоден наявний тест
цього не ловив: усі admin-тести підміняють _test_fetch monkeypatch'ем, тож
справжня from_row у цьому шляху ніколи не викликалась.
"""

import pytest

from worker.fetchers import base


def _row(**over) -> dict:
    row = {
        "id": 1, "type": "discourse", "name": "Cardano Forum",
        "ecosystem": "Cardano", "url": "https://forum.cardano.org",
        "category": None, "config": {}, "lane": "rfp",
    }
    row.update(over)
    return row


def test_from_row_accepts_id_zero_for_an_unsaved_candidate():
    """id=0 — легальний кандидат з admin/app.py::add_source, а не «missing»."""
    source = base.Source.from_row(_row(id=0))
    assert source.id == 0
    assert source.name == "Cardano Forum"


@pytest.mark.parametrize("field", ["id", "type", "name", "ecosystem", "url"])
def test_from_row_rejects_none(field):
    with pytest.raises(ValueError, match=field):
        base.Source.from_row(_row(**{field: None}))


@pytest.mark.parametrize("field", ["type", "name", "ecosystem", "url"])
def test_from_row_rejects_empty_string(field):
    with pytest.raises(ValueError, match=field):
        base.Source.from_row(_row(**{field: ""}))


def test_from_row_rejects_a_missing_key_entirely():
    row = _row()
    del row["ecosystem"]
    with pytest.raises(ValueError, match="ecosystem"):
        base.Source.from_row(row)


def test_from_row_still_rejects_a_relative_url():
    with pytest.raises(ValueError, match="not absolute"):
        base.Source.from_row(_row(url="forum.cardano.org"))
