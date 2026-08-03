"""Generic REST aggregator — one fetcher, many APIs, mapping in config.

Each concrete aggregator (DAOstar OpenGrants, Karma GAP, Subsquare, Lidonation,
CryptoRank …) is a `sources` row whose config describes where the items live in
the response and which fields mean what:

    {
      "params":     {"limit": "50"},              // query string
      "headers":    {"X-Api-Key": "$CRYPTORANK_API_KEY"},  // $VAR from worker env
      "items_path": "data",                        // dot path to the item list
      "fields": {
        "external_id": "id",                       // required
        "title":       "name",                     // required
        "url":         "governanceURI",            // dot paths allowed
        "body":        "description",
        "ts":          "closeDate",                // ISO 8601 or epoch
        "ecosystem":   "extensions.platform"       // optional override
      },
      "require":    {"isOpen": true},              // equality filters
      "url_base":   "https://…"                    // prefix for relative urls
    }

New aggregator of this shape = one form submission, no deploy (§5).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterable

from ..http import HttpClient
from ..items import RawItem
from .base import Source

log = logging.getLogger(__name__)


def _dig(obj: Any, path: str) -> Any:
    """Follow a dot path through dicts and single-element unwraps."""
    current = obj
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Heuristic: epoch millis vs seconds.
        seconds = value / 1000 if value > 1e12 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, dict) and "$date" in value:  # Mongo-style (Karma GAP)
        value = value["$date"]
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _resolve_headers(raw: dict[str, str]) -> dict[str, str]:
    """`$VAR` values come from the worker environment, never from the DB.

    The DB row (form-editable) says *which* key a source needs; the secret
    itself stays in the worker env — single owner per key (§9).
    """
    headers = {}
    for name, value in raw.items():
        if isinstance(value, str) and value.startswith("$"):
            resolved = os.environ.get(value[1:], "")
            if not resolved:
                log.warning("header %s references unset env %s", name, value)
                continue
            value = resolved
        headers[name] = value
    return headers


def fetch(source: Source, client: HttpClient, since: datetime) -> Iterable[RawItem]:
    cfg = source.config
    fields: dict[str, str] = cfg.get("fields") or {}
    if "external_id" not in fields or "title" not in fields:
        raise ValueError("rest_aggregator needs config.fields.external_id and .title")

    url = source.url
    params = cfg.get("params") or {}
    if params:
        from urllib.parse import urlencode

        joiner = "&" if "?" in url else "?"
        url = f"{url}{joiner}{urlencode(params)}"

    response = client.get(url, headers=_resolve_headers(cfg.get("headers") or {}))
    if response.not_modified:
        return

    payload = response.json()
    items = _dig(payload, cfg["items_path"]) if cfg.get("items_path") else payload
    if items is None:
        raise ValueError(f"items_path `{cfg.get('items_path')}` matched nothing")
    if not isinstance(items, list):
        raise ValueError("items_path did not resolve to a list")

    require: dict[str, Any] = cfg.get("require") or {}
    url_base = cfg.get("url_base") or ""
    emitted = 0

    for entry in items:
        if any(_dig(entry, key) != expected for key, expected in require.items()):
            continue

        external_id = _dig(entry, fields["external_id"])
        title = _dig(entry, fields["title"])
        if external_id is None or not title:
            continue

        ts = _parse_ts(_dig(entry, fields["ts"])) if "ts" in fields else None
        if ts and ts < since:
            continue

        item_url = str(_dig(entry, fields.get("url", "")) or "")
        if item_url.startswith("/") and url_base:
            item_url = url_base.rstrip("/") + item_url

        yield RawItem(
            external_id=str(external_id),
            title=str(title),
            url=item_url or source.url,
            body=str(_dig(entry, fields.get("body", "")) or ""),
            ts=ts,
            ecosystem=(
                str(_dig(entry, fields["ecosystem"]))
                if "ecosystem" in fields and _dig(entry, fields["ecosystem"])
                else None
            ),
        )
        emitted += 1

    log.info("%s: %d item(s) after filters", source.name, emitted)
