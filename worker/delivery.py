"""Delivery to n8n, and the retry sweep that makes it at-least-once (A1).

The webhook's 200 is *not* delivery. An item stays `pending` until the n8n
workflow's final node — which runs only after the Pipedrive lead exists —
writes `done` back to `seen_items`. If n8n dies mid-run, Pipedrive 500s, or the
classifier misfires, the item is still pending and gets re-sent; the workflow's
idempotency check on item_uid makes that safe.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

import httpx
import psycopg

from .alerts import alert
from .config import config
from .items import Item

log = logging.getLogger(__name__)


def _post(payload: dict[str, Any]) -> None:
    response = httpx.post(
        config.n8n_webhook_url,
        json=payload,
        headers={
            # Shared-secret auth (A4): unsigned POSTs are rejected by the
            # workflow, which is what stops anyone burning the Claude budget or
            # injecting fake leads into the CRM.
            "X-Webhook-Secret": config.n8n_webhook_secret,
            "Content-Type": "application/json",
        },
        timeout=config.http_timeout_seconds,
    )
    response.raise_for_status()


def _record_payload(conn: psycopg.Connection, item: Item) -> None:
    """Keep the full payload so a retry does not need a re-fetch.

    `seen_items` stores identity, not content; without this a retry hours later
    would have to hit the source again just to rebuild the same body.
    """
    conn.execute(
        "INSERT INTO items_log (item_uid, source_id, event, payload) "
        "VALUES (%s, %s, 'fetched', %s)",
        (item.item_uid, item.source_id, json.dumps(item.to_payload())),
    )


def deliver(conn: psycopg.Connection, items: Iterable[Item]) -> int:
    """Send freshly-seen items. Returns how many were accepted by the webhook."""
    sent = 0
    for item in items:
        _record_payload(conn, item)
        conn.commit()
        try:
            _post(item.to_payload())
        except httpx.HTTPError as exc:
            log.warning("webhook rejected %s: %s", item.item_uid, exc)
            conn.execute(
                "UPDATE seen_items SET attempts = attempts + 1, last_attempt_at = now() "
                "WHERE item_uid = %s",
                (item.item_uid,),
            )
            conn.commit()
            continue

        conn.execute(
            "UPDATE seen_items SET attempts = attempts + 1, last_attempt_at = now() "
            "WHERE item_uid = %s",
            (item.item_uid,),
        )
        conn.commit()
        sent += 1

    return sent


def retry_pending(conn: psycopg.Connection) -> tuple[int, int]:
    """Re-send items n8n never confirmed. Returns (resent, newly_dead).

    Anything that outlives MAX_DELIVERY_ATTEMPTS becomes `dead` and is alerted —
    a lead that silently stopped being retried is the failure this whole state
    machine exists to prevent.
    """
    rows = conn.execute(
        """
        SELECT s.item_uid, s.attempts, s.title,
               (SELECT payload FROM items_log l
                 WHERE l.item_uid = s.item_uid AND l.event = 'fetched'
                 ORDER BY l.id DESC LIMIT 1) AS payload
          FROM seen_items s
         WHERE s.status = 'pending'
           AND (s.last_attempt_at IS NULL
                OR s.last_attempt_at < now() - make_interval(mins => %s))
         ORDER BY s.first_seen
         LIMIT 200
        """,
        (config.pending_retry_after_minutes,),
    ).fetchall()

    resent = 0
    dead: list[str] = []

    for row in rows:
        if row["attempts"] >= config.max_delivery_attempts:
            conn.execute(
                "UPDATE seen_items SET status = 'dead' WHERE item_uid = %s",
                (row["item_uid"],),
            )
            conn.commit()
            dead.append(f"{row['title'] or row['item_uid']}")
            continue

        if not row["payload"]:
            # No stored payload means the row predates the log or the log was
            # pruned. It cannot be rebuilt here, so retiring it is honest.
            conn.execute(
                "UPDATE seen_items SET status = 'dead' WHERE item_uid = %s",
                (row["item_uid"],),
            )
            conn.commit()
            dead.append(f"{row['item_uid']} (no stored payload)")
            continue

        try:
            _post(row["payload"])
            resent += 1
        except httpx.HTTPError as exc:
            log.warning("retry failed for %s: %s", row["item_uid"], exc)

        conn.execute(
            "UPDATE seen_items SET attempts = attempts + 1, last_attempt_at = now() "
            "WHERE item_uid = %s",
            (row["item_uid"],),
        )
        conn.commit()

    if dead:
        alert(
            f"{len(dead)} item(s) exhausted delivery attempts and were marked dead — "
            "these will never reach Pipedrive without manual action:\n"
            + "\n".join(f"• {d}" for d in dead[:10]),
            level="error",
        )
    if resent:
        log.info("re-sent %d pending item(s)", resent)

    return resent, len(dead)
