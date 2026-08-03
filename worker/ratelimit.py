"""Shared per-host token bucket, held in Postgres.

State lives in the database rather than in process memory because two different
consumers hit the same forums: the hourly pipeline fetchers and (Stage 5) the KB
crawler. Independent limiters would double the effective rate on exactly the
hosts most likely to sit behind Cloudflare (KB-Module-Design.md §2).
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

import psycopg

from .config import config

log = logging.getLogger(__name__)

# A single wait is capped so a misconfigured bucket cannot stall a run.
MAX_WAIT_SECONDS = 30.0


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "unknown").lower()


def acquire(conn: psycopg.Connection, url: str, tokens: float = 1.0) -> float:
    """Consume `tokens` for the URL's host, sleeping if the bucket is empty.

    Returns the seconds spent waiting. The row is locked FOR UPDATE, so
    concurrent workers serialise on the host they are about to hit.
    """
    host = host_of(url)
    waited = 0.0

    for _ in range(2):
        # Close any transaction the caller left open (e.g. http_cache SELECTs).
        # Inside an open transaction the `with conn.transaction()` below is only
        # a SAVEPOINT, and the FOR UPDATE row lock would then survive the block
        # and be held across the caller's network I/O — blocking every other
        # consumer of this host (the exact problem the shared bucket solves).
        conn.commit()
        with conn.transaction():
            row = conn.execute(
                "SELECT tokens, rate_per_sec, burst, last_refill "
                "FROM host_rate_limits WHERE host = %s FOR UPDATE",
                (host,),
            ).fetchone()

            if row is None:
                conn.execute(
                    "INSERT INTO host_rate_limits (host, tokens, rate_per_sec, burst) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (host) DO NOTHING",
                    (
                        host,
                        config.host_rate_limit_burst - tokens,
                        config.host_rate_limit_rps,
                        config.host_rate_limit_burst,
                    ),
                )
                return waited

            elapsed_row = conn.execute(
                "SELECT EXTRACT(EPOCH FROM (now() - %s))::float8 AS elapsed",
                (row["last_refill"],),
            ).fetchone()
            elapsed = max(0.0, elapsed_row["elapsed"])
            available = min(
                row["burst"], row["tokens"] + elapsed * row["rate_per_sec"]
            )

            if available >= tokens:
                conn.execute(
                    "UPDATE host_rate_limits SET tokens = %s, last_refill = now() "
                    "WHERE host = %s",
                    (available - tokens, host),
                )
                return waited

            deficit = tokens - available
            sleep_for = min(MAX_WAIT_SECONDS, deficit / max(row["rate_per_sec"], 0.01))

        conn.commit()  # the lock must be gone before we sleep
        # Sleep outside the transaction — holding a row lock while waiting would
        # block every other consumer of the same host for no reason.
        log.debug("rate limit: waiting %.2fs for %s", sleep_for, host)
        time.sleep(sleep_for)
        waited += sleep_for

    # Two passes and still short: the bucket is contended beyond MAX_WAIT.
    # Proceed rather than stall the run — the wait already spent is the throttle.
    log.warning("rate limit for %s not satisfied after %.1fs; proceeding", host, waited)
    return waited
