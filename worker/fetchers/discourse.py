"""Discourse forums — the Tier-1 workhorse.

Anonymous JSON endpoints only (`/c/<slug>/<id>.json`, `/t/<id>.json`), which is
what the forums' robots.txt permits; `/search` and `.rss` are disallowed for
automated use and are deliberately not touched here.

The rate risk is not Discourse's own limit (generous at 200/min/IP) but
Cloudflare blocking datacenter IPs — hence the honest User-Agent, the shared
token bucket, and 403 surfacing as SourceBlocked rather than a silent zero.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from ..http import FetchError, HttpClient
from ..items import RawItem, strip_html
from .base import Source

log = logging.getLogger(__name__)

# One page is 30 topics; two pages covers even the busiest governance category
# between hourly runs with room to spare.
MAX_PAGES = 2


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _topic_body(client: HttpClient, base: str, topic_id: int) -> str:
    """First post of the thread — the RFP itself, not the discussion under it."""
    try:
        payload = client.get(f"{base}/t/{topic_id}.json", use_cache=False).json()
    except (FetchError, ValueError) as exc:
        log.warning("could not fetch topic %s on %s: %s", topic_id, base, exc)
        return ""

    posts = (payload.get("post_stream") or {}).get("posts") or []
    return strip_html(posts[0].get("cooked", "")) if posts else ""


def fetch(source: Source, client: HttpClient, since: datetime) -> Iterable[RawItem]:
    categories: list[dict[str, Any]] = source.config.get("categories") or []
    if not categories:
        raise ValueError("discourse source needs config.categories = [{slug, id}]")

    for category in categories:
        slug, category_id = category.get("slug"), category.get("id")
        if not slug or category_id is None:
            raise ValueError(f"malformed category entry: {category!r}")

        for page in range(MAX_PAGES):
            url = f"{source.url}/c/{slug}/{category_id}.json?page={page}"
            response = client.get(url)
            if response.not_modified:
                log.debug("%s/%s page %d unchanged", source.name, slug, page)
                break

            topics = ((response.json().get("topic_list") or {}).get("topics")) or []
            if not topics:
                break

            stale_page = True
            for topic in topics:
                created = _parse_ts(topic.get("created_at"))
                bumped = _parse_ts(topic.get("bumped_at"))
                # Bump time, not creation time: an RFP thread edited with a new
                # deadline is worth re-reading, and content_hash decides whether
                # anything actually changed.
                latest = bumped or created
                if latest is None or latest < since:
                    # Pinned topics sit at the top of every category forever;
                    # this is also what stops them re-triggering every run.
                    continue

                stale_page = False
                topic_id = topic["id"]
                title = topic.get("title") or ""
                topic_slug = topic.get("slug") or "topic"
                topic_url = f"{source.url}/t/{topic_slug}/{topic_id}"

                # external_id is the bare topic id: it is unique per forum, and
                # a topic listed both in a parent category and its subcategory
                # (Optimism grants → gov-fund-missions) must collapse to one uid.
                yield RawItem(
                    external_id=str(topic_id),
                    title=title,
                    url=topic_url,
                    body=_topic_body(client, source.url, topic_id),
                    ts=created or bumped,
                    watermark_ts=bumped or created,
                    extra={
                        "category": slug,
                        "posts_count": topic.get("posts_count"),
                        "bumped_at": topic.get("bumped_at"),
                    },
                )

            # Pages are ordered newest-first: once a whole page predates the
            # watermark there is nothing older worth walking to.
            if stale_page:
                break
