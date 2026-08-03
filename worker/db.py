"""Postgres access and the migration runner."""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from .config import config

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """A transaction-scoped connection with dict rows.

    client_encoding is pinned: forum titles are full of non-ASCII, and on a
    cluster initialised with a C locale the default would be SQL_ASCII and the
    first em-dash in a title would abort the run.
    """
    with psycopg.connect(
        config.database_url, row_factory=dict_row, client_encoding="utf8"
    ) as conn:
        yield conn


def _substitute(sql: str) -> str:
    """Fill identifier placeholders that cannot be passed as query parameters.

    Only ${N8N_DB_USER} is supported, and only if it is a bare identifier —
    this is DDL text interpolation, so the validation is the whole safety story.
    """
    import os

    role = os.environ.get("N8N_DB_USER", "n8n").strip()
    if not _IDENTIFIER.match(role):
        raise RuntimeError(f"N8N_DB_USER is not a plain SQL identifier: {role!r}")
    return sql.replace("${N8N_DB_USER}", role)


def apply_migrations(migrations_dir: Path | None = None) -> list[str]:
    """Apply unapplied .sql files in filename order. Each file is one transaction."""
    directory = migrations_dir or MIGRATIONS_DIR
    files = sorted(p for p in directory.glob("*.sql"))
    applied: list[str] = []

    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename   text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        conn.commit()

        done = {
            row["filename"]
            for row in conn.execute("SELECT filename FROM schema_migrations")
        }

        for path in files:
            if path.name in done:
                continue
            log.info("applying migration %s", path.name)
            try:
                conn.execute(_substitute(path.read_text()))
                conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            applied.append(path.name)

    return applied
