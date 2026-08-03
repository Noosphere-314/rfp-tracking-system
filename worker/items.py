"""Normalised items and the three identities the pipeline depends on."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Words that carry no distinguishing signal in a governance-forum title and
# would otherwise keep near-identical cross-posts from collapsing.
_FINGERPRINT_STOPWORDS = {
    "a", "an", "and", "the", "for", "of", "to", "in", "on", "at", "by", "with",
    "rfp", "rfc", "proposal", "draft", "temp", "check", "discussion", "final",
    "grant", "grants", "request", "call",
}
_NON_WORD = re.compile(r"[^a-z0-9\s]+")
_WHITESPACE = re.compile(r"\s+")
_TAGS = re.compile(r"<[^>]+>")


@dataclass
class RawItem:
    """What a fetcher returns, before identity is computed."""

    external_id: str
    title: str
    url: str
    body: str = ""
    ts: datetime | None = None
    ecosystem: str | None = None
    # When this item last *changed* at the source (e.g. Discourse bumped_at).
    # Drives the source watermark; `ts` stays the creation time. Without the
    # distinction, a bump on an old topic drags the watermark months back and
    # every following run re-reads that whole span.
    watermark_ts: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Item:
    """A fetched item with its identities resolved. This is what n8n receives."""

    item_uid: str
    source_id: int
    source_name: str
    source_type: str
    external_id: str
    title: str
    url: str
    body: str
    ecosystem: str
    lane: str
    ts: datetime | None
    watermark_ts: datetime | None
    content_hash: str
    fingerprint: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "item_uid": self.item_uid,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "source_type": self.source_type,
            "external_id": self.external_id,
            "title": self.title,
            "url": self.url,
            # Truncated: the classifier reads the opening of a thread, and an
            # unbounded body would blow up both the webhook payload and the
            # token bill on the occasional 40k-word governance essay.
            "body": self.body[:8000],
            "ecosystem": self.ecosystem,
            "lane": self.lane,
            "ts": self.ts.isoformat() if self.ts else None,
            "content_hash": self.content_hash,
            "fingerprint": self.fingerprint,
        }


def strip_html(html: str) -> str:
    if not html:
        return ""
    text = _TAGS.sub(" ", html)
    for entity, char in (
        ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
        ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " "),
    ):
        text = text.replace(entity, char)
    return _WHITESPACE.sub(" ", text).strip()


def _fold(text: str) -> str:
    """Lowercase and strip diacritics, so `Café` and `Cafe` are one word."""
    folded = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in folded if not unicodedata.combining(c))


def make_item_uid(source_id: int, external_id: str) -> str:
    """Primary identity: stable per (source, item). Drives the PK dedup."""
    return hashlib.sha256(f"{source_id}:{external_id}".encode()).hexdigest()


def make_content_hash(title: str, body: str) -> str:
    """Change detection: a moved deadline or a closed status changes this.

    Phase 2 turns a change on an already-delivered RFP into a note on the
    existing lead (A11), which is why it is stored from day one.
    """
    normalized = _WHITESPACE.sub(" ", f"{title}\n{body}".strip().lower())
    return hashlib.sha256(normalized.encode()).hexdigest()


def make_fingerprint(title: str, ecosystem: str) -> str:
    """Cross-source identity (A6).

    The same RFP appears on a forum, on Snapshot and in an aggregator with three
    different external ids. The fingerprint is a normalised bag of the title's
    significant words plus the ecosystem, so those three collapse to one lead
    with notes rather than three leads.

    This is the deterministic first pass and it only catches copies that agree
    on the ecosystem label — a Snapshot space named "Arbitrum DAO" will not
    match a forum row labelled "Arbitrum". The classifier's `canonical_project`
    and `canonical_title` close that gap downstream (A6).
    """
    eco = (ecosystem or "").lower()
    # The ecosystem is already in the basis. Forum titles often repeat it
    # ("[RFP] Arbitrum — ...") while Snapshot titles do not, and that alone
    # would keep the two copies of one RFP apart.
    eco_words = set(_NON_WORD.sub(" ", _fold(eco)).split())

    words = _NON_WORD.sub(" ", _fold(title)).split()
    significant = sorted(
        {
            w
            for w in words
            if w not in _FINGERPRINT_STOPWORDS and w not in eco_words and len(w) > 2
        }
    )
    basis = f"{eco}:{' '.join(significant)}"
    return hashlib.sha256(basis.encode()).hexdigest()


def build(
    raw: RawItem,
    *,
    source_id: int,
    source_name: str,
    source_type: str,
    ecosystem: str,
    lane: str,
) -> Item:
    resolved_ecosystem = raw.ecosystem or ecosystem
    title = _WHITESPACE.sub(" ", raw.title or "").strip()
    body = strip_html(raw.body or "")
    ts = raw.ts
    if ts is not None and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    watermark_ts = raw.watermark_ts or ts
    if watermark_ts is not None and watermark_ts.tzinfo is None:
        watermark_ts = watermark_ts.replace(tzinfo=timezone.utc)

    return Item(
        item_uid=make_item_uid(source_id, raw.external_id),
        source_id=source_id,
        source_name=source_name,
        source_type=source_type,
        external_id=raw.external_id,
        title=title,
        url=raw.url,
        body=body,
        ecosystem=resolved_ecosystem,
        lane=lane,
        ts=ts,
        watermark_ts=watermark_ts,
        content_hash=make_content_hash(title, body),
        fingerprint=make_fingerprint(title, resolved_ecosystem),
    )
