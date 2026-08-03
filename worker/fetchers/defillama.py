"""DefiLlama /protocols — TVL-spike detection for the FUNDING lane (A8).

A TVL spike is a BD heuristic, not an RFP: a protocol growing 50%+ in a week
has budget and momentum, which makes it outbound material. These items always
carry lane='funding' and face a higher confidence bar downstream — they must
never dilute RFP-lane precision.

config:
    {
      "min_tvl": 1000000,        // USD floor — spikes on dust are noise
      "min_change_7d": 50,       // percent
      "max_items": 15            // per run, ranked by change
    }

Identity: one item per protocol per ISO week. A protocol that keeps spiking
produces at most one lead a week, not one per hourly run.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from ..http import HttpClient
from ..items import RawItem
from .base import Source

log = logging.getLogger(__name__)


def fetch(source: Source, client: HttpClient, since: datetime) -> Iterable[RawItem]:
    cfg = source.config
    min_tvl = float(cfg.get("min_tvl", 1_000_000))
    min_change = float(cfg.get("min_change_7d", 50))
    max_items = int(cfg.get("max_items", 15))

    response = client.get(source.url, use_cache=False)  # ~10 MB, changes every call
    protocols = response.json()
    if not isinstance(protocols, list):
        raise ValueError("unexpected /protocols response shape")

    now = datetime.now(timezone.utc)
    week = now.strftime("%G-W%V")

    spikes = []
    for protocol in protocols:
        tvl = protocol.get("tvl") or 0
        change = protocol.get("change_7d")
        if change is None or tvl < min_tvl or change < min_change:
            continue
        spikes.append((change, tvl, protocol))

    spikes.sort(key=lambda entry: entry[0], reverse=True)
    log.info("defillama: %d spike(s) above +%.0f%% & $%.0f", len(spikes), min_change, min_tvl)

    for change, tvl, protocol in spikes[:max_items]:
        slug = protocol.get("slug") or protocol.get("name", "").lower()
        name = protocol.get("name") or slug
        yield RawItem(
            external_id=f"{slug}:{week}",
            title=f"TVL spike: {name} +{change:.0f}% in 7d (${tvl/1e6:.1f}M TVL)",
            url=protocol.get("url") or f"https://defillama.com/protocol/{slug}",
            body=(
                f"{protocol.get('description') or ''}\n\n"
                f"Chain: {protocol.get('chain')} · Category: {protocol.get('category')} · "
                f"TVL: ${tvl:,.0f} · 1d: {protocol.get('change_1d')}% · 7d: {change:.1f}%"
            ).strip(),
            ts=now,
            ecosystem=protocol.get("chain") or "multi",
            extra={"category": protocol.get("category"), "tvl": tvl},
        )
