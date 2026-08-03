"""Fetcher contract.

One module per *source type*, never per source. A new concrete source of an
existing type is a row in `sources` — a form submission, not a deploy
(System-Design.md §5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Protocol

from ..http import HttpClient
from ..items import RawItem


@dataclass
class Source:
    """A validated `sources` row."""

    id: int
    type: str
    name: str
    ecosystem: str
    url: str
    category: str | None
    config: dict[str, Any]
    lane: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Source":
        """Validate-on-read (A4).

        Raises ValueError on a row the fetcher layer cannot act on; the caller
        quarantines it and alerts instead of skipping it silently.
        """
        for field_name in ("id", "type", "name", "ecosystem", "url"):
            if not row.get(field_name):
                raise ValueError(f"missing required column `{field_name}`")

        url = str(row["url"])
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"url is not absolute: {url!r}")

        config = row.get("config") or {}
        if not isinstance(config, dict):
            raise ValueError("config is not a JSON object")

        return cls(
            id=int(row["id"]),
            type=str(row["type"]),
            name=str(row["name"]),
            ecosystem=str(row["ecosystem"]),
            url=url.rstrip("/"),
            category=row.get("category"),
            config=config,
            lane=row.get("lane") or "rfp",
        )


class Fetcher(Protocol):
    """Fetchers are pure: they read, they yield, they do not touch state.

    Deciding what is new, what gets delivered and what gets retried belongs to
    the pipeline — a fetcher that also owned dedup would have to be trusted, and
    the fragile ones (scrapers) are exactly the ones that should not be.
    """

    def fetch(
        self, source: Source, client: HttpClient, since: datetime
    ) -> Iterable[RawItem]:
        ...
