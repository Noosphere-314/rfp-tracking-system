"""Спільна fake-заміна psycopg-з'єднання для тестів kbtools.py/chat.py — без
жодного реального Postgres.

Кожен тест дає РОУТЕР: список пар (підрядок SQL, відповідь); перший збіг за
порядком перемагає. Усі execute()-виклики записуються в calls — зручно
перевіряти, який запит пішов і з якими параметрами (наприклад, який
OR-запит зібрав chat._stub_query і передав у websearch_to_tsquery).
"""

from __future__ import annotations


class FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows or [])

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        # Реальний psycopg-курсор ітерується напряму (briefing.make_brief's
        # `for r in conn.execute(...)` для archived_ecosystems) — без цього
        # той шлях падає з "FakeCursor object is not iterable".
        return iter(self._rows)


class FakeConn:
    def __init__(self, router, calls):
        self._router = router
        self._calls = calls
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._calls.append((sql, params))
        for marker, response in self._router:
            if marker in sql:
                rows = response(params) if callable(response) else response
                return FakeCursor(rows)
        # Немає підрядка-збігу — типово для запитів, байдужих для цього
        # тесту (наприклад kb.query_log, коли тест перевіряє лише пошук).
        # Порожній курсор замість падіння: тест роутить тільки те, що йому
        # цікаво перевірити.
        return FakeCursor([])

    def commit(self):
        self.committed = True


def make_db(router):
    """router: list[(substring, rows | callable(params) -> rows)].

    Повертає (db_factory, calls): db_factory — підміна kbtools._db (виклик
    без аргументів повертає нове fake-з'єднання, як і справжній psycopg._db),
    calls — спільний список усіх (sql, params) у порядку виконання.
    """
    calls: list = []

    def db_factory():
        return FakeConn(router, calls)

    return db_factory, calls
