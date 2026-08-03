"""GitHub Discussions via GraphQL — Filecoin community, TON grants repos.

Needs a free read-only PAT in GITHUB_TOKEN (worker env). Sources without a
token configured fail loudly at fetch time rather than silently returning
nothing — a quiet auth failure would look exactly like a dead source.

config:
    {"repos": [{"owner": "filecoin-project", "name": "community"}],
     "category": "RFPs"}        // optional discussion-category name filter
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable

from ..config import config
from ..http import HttpClient
from ..items import RawItem
from .base import Source

log = logging.getLogger(__name__)

API = "https://api.github.com/graphql"

QUERY = """
query Discussions($owner: String!, $name: String!, $first: Int!) {
  repository(owner: $owner, name: $name) {
    discussions(first: $first, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number title url bodyText createdAt updatedAt
        category { name }
      }
    }
  }
}
"""

PAGE_SIZE = 50


def fetch(source: Source, client: HttpClient, since: datetime) -> Iterable[RawItem]:
    if not config.github_token:
        raise ValueError("github_discussions requires GITHUB_TOKEN in the worker env")

    repos = source.config.get("repos") or []
    if not repos:
        raise ValueError("github_discussions needs config.repos = [{owner, name}]")
    category_filter = (source.config.get("category") or "").lower()

    headers = {"Authorization": f"Bearer {config.github_token}"}

    for repo in repos:
        owner, name = repo.get("owner"), repo.get("name")
        if not owner or not name:
            raise ValueError(f"malformed repo entry: {repo!r}")

        response = client.post_json(
            API,
            {"query": QUERY, "variables": {"owner": owner, "name": name, "first": PAGE_SIZE}},
            headers=headers,
        )
        payload = response.json()
        if payload.get("errors"):
            raise ValueError(f"github graphql errors for {owner}/{name}: {payload['errors']}")

        repository = (payload.get("data") or {}).get("repository") or {}
        nodes = ((repository.get("discussions") or {}).get("nodes")) or []

        for node in nodes:
            category = ((node.get("category") or {}).get("name") or "").lower()
            if category_filter and category_filter not in category:
                continue

            updated = datetime.fromisoformat(node["updatedAt"].replace("Z", "+00:00"))
            if updated < since:
                continue

            yield RawItem(
                external_id=f"{owner}/{name}#{node['number']}",
                title=node.get("title") or "",
                url=node.get("url") or "",
                body=node.get("bodyText") or "",
                ts=datetime.fromisoformat(node["createdAt"].replace("Z", "+00:00")),
                extra={"category": (node.get("category") or {}).get("name")},
            )
