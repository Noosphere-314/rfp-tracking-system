"""RSS/Atom feeds — the funding-signal lane and forum fallbacks.

These sources feed the FUNDING lane, which is held to a higher confidence bar
than the RFP lane: a funding round is a BD heuristic, not a request for
proposals, and it must not dilute the inbox (A8).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

import feedparser

from ..http import HttpClient
from ..items import RawItem, strip_html
from .base import Source

log = logging.getLogger(__name__)


def _entry_ts(entry) -> datetime | None:
    parsed = getattr(entry, "published_parsed", None) or getattr(
        entry, "updated_parsed", None
    )
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)


def fetch(source: Source, client: HttpClient, since: datetime) -> Iterable[RawItem]:
    response = client.get(source.url)
    if response.not_modified:
        log.debug("%s unchanged since last fetch", source.name)
        return

    feed = feedparser.parse(response.text)
    if feed.bozo and not feed.entries:
        raise ValueError(f"unparseable feed: {feed.bozo_exception}")

    for entry in feed.entries:
        ts = _entry_ts(entry)
        if ts and ts < since:
            continue

        body = ""
        if getattr(entry, "content", None):
            body = entry.content[0].get("value", "")
        body = body or getattr(entry, "summary", "")

        # Feed GUIDs are the stable identity when present; some feeds reuse or
        # omit them, in which case the permalink is the next best thing.
        external_id = getattr(entry, "id", "") or getattr(entry, "link", "")
        if not external_id:
            continue

        yield RawItem(
            external_id=external_id,
            title=getattr(entry, "title", "") or "",
            url=getattr(entry, "link", "") or "",
            body=strip_html(body),
            ts=ts,
            extra={"author": getattr(entry, "author", None)},
        )
