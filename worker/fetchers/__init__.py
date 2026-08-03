"""Fetcher registry.

Seven types cover every source on the audited list (System-Design.md §5).
Implemented: discourse, snapshot, rss, rest_aggregator, defillama,
github_discussions. Still to come, each ~50–100 lines plus a line here:

    questbook   Domain Allocator programs — needs the Stage-4 auth spike
    scraper     OP Atlas, EF ESP — no API, most fragile, dark-source alert
"""

from __future__ import annotations

from typing import Callable

from . import defillama, discourse, github_discussions, rest_aggregator, rss, snapshot
from .base import Source  # re-exported for callers

FETCHERS: dict[str, Callable] = {
    "discourse": discourse.fetch,
    "snapshot": snapshot.fetch,
    "rss": rss.fetch,
    "rest_aggregator": rest_aggregator.fetch,
    "defillama": defillama.fetch,
    "github_discussions": github_discussions.fetch,
}


def get(source_type: str) -> Callable:
    try:
        return FETCHERS[source_type]
    except KeyError:
        raise ValueError(f"no fetcher registered for type `{source_type}`") from None


__all__ = ["FETCHERS", "Source", "get"]
