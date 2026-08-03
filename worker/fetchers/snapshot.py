"""Snapshot — one GraphQL endpoint covering a curated allowlist of spaces.

Keyword filtering happens client-side (in the shared pre-filter), because the
API has no text search worth relying on. The free key raises 100 req/min to
~2M req/month; without it the same query still works, just closer to the limit.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from ..config import config
from ..http import HttpClient
from ..items import RawItem
from .base import Source

log = logging.getLogger(__name__)

QUERY = """
query Proposals($spaces: [String!], $createdGte: Int!, $first: Int!) {
  proposals(
    first: $first
    skip: 0
    where: { space_in: $spaces, created_gte: $createdGte }
    orderBy: "created"
    orderDirection: desc
  ) {
    id
    title
    body
    created
    start
    end
    link
    state
    space { id name }
  }
}
"""

PAGE_SIZE = 100


def fetch(source: Source, client: HttpClient, since: datetime) -> Iterable[RawItem]:
    spaces = source.config.get("spaces") or []
    if not spaces:
        raise ValueError("snapshot source needs config.spaces = [space ids]")

    headers = {}
    if config.snapshot_api_key:
        headers["x-api-key"] = config.snapshot_api_key

    response = client.post_json(
        source.url,
        {
            "query": QUERY,
            "variables": {
                "spaces": spaces,
                "createdGte": int(since.timestamp()),
                "first": PAGE_SIZE,
            },
        },
        headers=headers,
    )
    payload = response.json()

    if payload.get("errors"):
        raise ValueError(f"snapshot GraphQL errors: {payload['errors']}")

    proposals = (payload.get("data") or {}).get("proposals") or []
    log.info("snapshot returned %d proposals across %d spaces", len(proposals), len(spaces))

    for proposal in proposals:
        space = proposal.get("space") or {}
        yield RawItem(
            external_id=proposal["id"],
            title=proposal.get("title") or "",
            url=proposal.get("link") or f"https://snapshot.box/#/{space.get('id')}/proposal/{proposal['id']}",
            body=proposal.get("body") or "",
            ts=datetime.fromtimestamp(proposal["created"], tz=timezone.utc),
            # The space is the real ecosystem here, not the source's "multi".
            ecosystem=space.get("name") or space.get("id"),
            extra={"state": proposal.get("state"), "space": space.get("id")},
        )
