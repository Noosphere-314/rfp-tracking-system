"""The hourly run: load sources → fetch → identify → record → deliver."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

from . import delivery, fetchers, items as items_mod
from .alerts import alert, ping_healthchecks
from .config import config
from .fetchers.base import Source
from .filters import KeywordSet, load_keywords
from .http import FetchError, HttpClient, SourceBlocked

log = logging.getLogger(__name__)

# Re-read window on top of the watermark. Forum topics get edited and bumped
# after they are first seen, so a strict watermark would miss late changes.
WATERMARK_OVERLAP = timedelta(hours=1)
# Seed mode reaches back far enough to cover whatever history a source exposes.
SEED_LOOKBACK = timedelta(days=3650)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def load_sources(conn: psycopg.Connection) -> list[Source]:
    """Read enabled sources, quarantining any row that fails validation (A4).

    A malformed row added through a form is a data problem, not an outage: it
    is parked with a reason, alerted once, and the rest of the run continues.
    """
    rows = conn.execute(
        "SELECT * FROM sources WHERE enabled AND NOT quarantined ORDER BY id"
    ).fetchall()

    sources: list[Source] = []
    quarantined: list[str] = []

    for row in rows:
        try:
            source = Source.from_row(row)
            if source.type not in fetchers.FETCHERS:
                raise ValueError(f"unknown source type `{source.type}`")
            sources.append(source)
        except ValueError as exc:
            conn.execute(
                "UPDATE sources SET quarantined = true, quarantine_reason = %s WHERE id = %s",
                (str(exc), row["id"]),
            )
            quarantined.append(f"#{row['id']} {row.get('name') or '?'} — {exc}")

    if quarantined:
        conn.commit()
        alert(
            "source rows quarantined and skipped until fixed:\n"
            + "\n".join(f"• {q}" for q in quarantined),
            level="error",
        )

    log.info("loaded %d enabled source(s)", len(sources))
    return sources


def _watermark(row: dict[str, Any] | None, seed: bool) -> datetime:
    if seed:
        return _now() - SEED_LOOKBACK
    last = (row or {}).get("last_item_at") or (row or {}).get("last_success_at")
    if last is None:
        return _now() - timedelta(hours=config.fetch_lookback_hours)
    return last - WATERMARK_OVERLAP


def _watermark_marker(item: items_mod.Item, now: datetime) -> datetime | None:
    """Мітка часу айтема для водяного знака — або None, якщо їй не можна вірити.

    Дата з МАЙБУТНЬОГО отруює водяний знак назавжди. DAOstar мапить ts на
    closeDate (дедлайн пулу, тобто дата попереду): один пул із 2028-09-30
    підняв last_item_at у 2028 рік, і відтоді КОЖЕН реальний пул відсікався
    як «застарий». Найгірше — мовчки: перевірка темних джерел шукає
    last_item_at у минулому, тож джерело з датою в майбутньому не потрапляє
    в алерт узагалі (виправлено там же і в міграції 017).
    """
    marker = item.watermark_ts or item.ts
    if marker is None or marker > now:
        return None
    return marker


def _record(
    conn: psycopg.Connection,
    item: items_mod.Item,
    keywords: KeywordSet,
    seed: bool,
    prequalified: bool = False,
) -> str | None:
    """Insert the item if unseen. Returns its status, or None if already known.

    ON CONFLICT DO NOTHING on the item_uid primary key is the dedup: an item
    already in the table — whatever its status — is never re-delivered.
    """
    if seed:
        # A2: record history so it dedups forever, but never deliver it. Skipping
        # this step is what floods the CRM with hundreds of stale leads on day one.
        status = "seeded"
    elif prequalified:
        # The fetcher itself is the filter (defillama thresholds); running RFP
        # keywords over "TVL spike: …" would just silently drop the whole lane.
        status = "pending"
    else:
        passes, _ = keywords.matches(f"{item.title}\n{item.body}")
        status = "pending" if passes else "filtered"

    row = conn.execute(
        """
        INSERT INTO seen_items (item_uid, source_id, external_id, status, title,
                                url, content_hash, fingerprint, item_ts)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (item_uid) DO NOTHING
        RETURNING item_uid
        """,
        (
            item.item_uid,
            item.source_id,
            item.external_id,
            status,
            item.title[:500],
            item.url,
            item.content_hash,
            item.fingerprint,
            item.ts,
        ),
    ).fetchone()

    return status if row else None


def _fetch_source(
    conn: psycopg.Connection,
    client: HttpClient,
    source: Source,
    row: dict[str, Any],
    keywords: KeywordSet,
    seed: bool,
) -> dict[str, int]:
    stats = {"seen": 0, "new": 0, "filtered": 0, "delivered": 0}
    since = _watermark(row, seed)
    fetcher = fetchers.get(source.type)

    conn.execute("UPDATE sources SET last_attempt_at = now() WHERE id = %s", (source.id,))
    conn.commit()

    log.info("fetching %s (%s) since %s", source.name, source.type, since.isoformat())

    to_deliver: list[items_mod.Item] = []
    newest_ts: datetime | None = None
    now = _now()
    prequalified = bool(source.config.get("prequalified"))

    for raw in fetcher(source, client, since):
        stats["seen"] += 1
        item = items_mod.build(
            raw,
            source_id=source.id,
            source_name=source.name,
            source_type=source.type,
            ecosystem=source.ecosystem,
            lane=source.lane,
        )
        marker = _watermark_marker(item, now)
        if marker and (newest_ts is None or marker > newest_ts):
            newest_ts = marker

        status = _record(conn, item, keywords, seed, prequalified)
        if status is None:
            continue
        stats["new"] += 1
        if status == "filtered":
            stats["filtered"] += 1
        elif status == "pending":
            to_deliver.append(item)

    conn.commit()

    if to_deliver:
        stats["delivered"] = delivery.deliver(conn, to_deliver)

    # NULL newest_ts must leave last_item_at untouched — coalescing it into the
    # GREATEST would stamp epoch (1970) on sources whose items carry no dates.
    conn.execute(
        """
        UPDATE sources
           SET last_success_at = now(),
               consecutive_failures = 0,
               -- Добір відбувся (навіть якщо нічого не знайшов) — більше не
               -- повторюємо. Провал сюди не доходить: там _record_failure,
               -- тож backfilled_at лишається порожнім і добір буде ще раз.
               backfilled_at = COALESCE(backfilled_at, now()),
               last_item_at = CASE
                   WHEN %s::timestamptz IS NULL THEN last_item_at
                   ELSE GREATEST(COALESCE(last_item_at, %s::timestamptz), %s::timestamptz)
               END
         WHERE id = %s
        """,
        (newest_ts, newest_ts, newest_ts, source.id),
    )
    conn.commit()

    return stats


# Скільки джерел перелічити поіменно; решта згортається в "…and N more",
# щоб «12 source(s)» зі списком на 10 не читалось як повний список.
DARK_LISTED = 10

# Тихі джерела не є аварією, тож дайджест про них — раз на стільки днів.
QUIET_DIGEST_DAYS = 7
# За цим префіксом шукаємо в alerts, коли дайджест виходив востаннє.
QUIET_PREFIX = "quiet sources: "


def _dark_sort_key(row: dict[str, Any]) -> tuple[bool, float, str]:
    """Джерела, які не дали нічого НІКОЛИ, — першими; далі від найдавнішого."""
    ts = row["last_item_at"]
    return (ts is not None, ts.timestamp() if ts else 0.0, row["name"])


def _source_lines(rows: list[dict[str, Any]]) -> list[str]:
    """Список джерел у детермінованому порядку, з чесним хвостом.

    Порядок тут, а не в SQL, бо саме готовий рядок є ключем дедупу в
    alerts._store — той порівнює message ПОБУКВЕНО. `ORDER BY last_item_at
    NULLS FIRST` не задає порядок серед джерел із порожнім last_item_at:
    Postgres повертав ті самі вісім щоразу інакше, рядок виходив новий, і
    дедуп його пропускав — 31.08.2026 той самий алерт прилетів у бот о
    17:24 і о 18:26.
    """
    ordered = sorted(rows, key=_dark_sort_key)
    lines = [f"• {d['name']} ({d['ecosystem']})" for d in ordered[:DARK_LISTED]]
    if len(ordered) > DARK_LISTED:
        lines.append(f"…and {len(ordered) - DARK_LISTED} more")
    return lines


def _dead_message(rows: list[dict[str, Any]]) -> str:
    return (
        f"{len(rows)} source(s) returned NOTHING on a full-history sweep — "
        "the endpoint likely moved or the category was renamed:\n"
        + "\n".join(_source_lines(rows))
    )


def _quiet_message(rows: list[dict[str, Any]], days: int) -> str:
    return (
        f"{QUIET_PREFIX}{len(rows)} source(s) still fetch fine but have "
        f"published nothing new for {days}+ days:\n"
        + "\n".join(_source_lines(rows))
    )


def _quiet_digest_due(conn: psycopg.Connection) -> bool:
    """Чи виходив дайджест тихих джерел за останні QUIET_DIGEST_DAYS."""
    return (
        conn.execute(
            "SELECT 1 FROM alerts WHERE message LIKE %s "
            "AND created_at > now() - make_interval(days => %s) LIMIT 1",
            (QUIET_PREFIX + "%", QUIET_DIGEST_DAYS),
        ).fetchone()
        is None
    )


def _check_dark_sources(conn: psycopg.Connection) -> None:
    """A3: a source that has gone quiet looks identical to one that is broken.

    Насправді — не зовсім, і 31.08.2026 ця різниця коштувала довіри до
    алерту. Вісім джерел у списку виглядали як аварія, а перевірка наживо
    показала: всі вісім віддають 200 і реальний контент, просто в тихій
    grants-категорії найновіша тема буває піврічної давнини. Алерт, який
    щогодини кричить правду, на яку нема чого відповісти, вимикають — і
    разом із ним перестають бачити справжні аварії.

    Тому два різні сигнали:

    МЕРТВЕ (щогодини, warning) — добір по всій історії вже відбувся і не
    приніс НІЧОГО, або водяний знак стоїть у майбутньому. На це є що
    робити: лізти й дивитись, куди переїхав ендпойнт.

    ТИХЕ (раз на тиждень, info) — джерело приносило айтеми, останній
    давніший за source_dark_days. Це нормальний стан гранту, який
    оголошують раз на квартал; знати корисно, будити щогодини — ні.
    """
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'source_dark_days'"
    ).fetchone()
    days = int(row["value"]) if row else 14

    dead = conn.execute(
        """
        SELECT name, ecosystem, last_item_at FROM sources
         WHERE enabled AND NOT quarantined
           -- Поки добору не було, «нічого немає» ще нічого не означає.
           AND backfilled_at IS NOT NULL
           AND (last_item_at IS NULL OR last_item_at > now())
         ORDER BY name
        """
    ).fetchall()
    if dead:
        alert(_dead_message(dead))

    quiet = conn.execute(
        """
        SELECT name, ecosystem, last_item_at FROM sources
         WHERE enabled AND NOT quarantined
           -- Дата в майбутньому сюди не потрапляє: вона більша за now(),
           -- отже й за now() - N днів. Такі джерела бере запит вище.
           AND last_item_at IS NOT NULL
           AND last_item_at < now() - make_interval(days => %s)
         ORDER BY last_item_at
        """,
        (days,),
    ).fetchall()
    if quiet and _quiet_digest_due(conn):
        alert(_quiet_message(quiet, days), level="info")


def _prune_items_log(conn: psycopg.Connection) -> None:
    conn.execute(
        "DELETE FROM items_log WHERE created_at < now() - make_interval(days => %s)",
        (config.items_log_retention_days,),
    )
    conn.commit()


# Any 64-bit constant; identifies "the pipeline run" lock across all workers.
RUN_LOCK_KEY = 8_274_119_004_512_337


class RunAlreadyInProgress(RuntimeError):
    """Another worker process is mid-run against this database."""


def run_once(conn: psycopg.Connection, *, seed: bool = False) -> dict[str, Any]:
    """One full pass over every enabled source.

    Guarded by a Postgres advisory lock: two concurrent runs corrupt state.
    A run that is fetching has not yet committed its payload to items_log, so a
    second run entering retry_pending at that moment sees a pending item with no
    payload and dead-letters a perfectly live lead. Observed in practice when a
    manual `worker run` overlapped the loop container.
    """
    got_lock = conn.execute(
        "SELECT pg_try_advisory_lock(%s) AS locked", (RUN_LOCK_KEY,)
    ).fetchone()["locked"]
    if not got_lock:
        raise RunAlreadyInProgress(
            "another worker run is in progress; refusing to run concurrently"
        )
    try:
        return _run_once_locked(conn, seed=seed)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (RUN_LOCK_KEY,))
        conn.commit()


def _run_once_locked(conn: psycopg.Connection, *, seed: bool = False) -> dict[str, Any]:
    mode = "seed" if seed else "run"
    run_id = conn.execute(
        "INSERT INTO worker_runs (mode) VALUES (%s) RETURNING id", (mode,)
    ).fetchone()["id"]
    conn.commit()

    keywords = load_keywords(conn)
    sources = load_sources(conn)
    totals = {
        "sources_checked": 0,
        "sources_failed": 0,
        "items_seen": 0,
        "items_new": 0,
        "items_filtered": 0,
        "items_delivered": 0,
        "items_dead": 0,
    }
    failures: list[str] = []

    with HttpClient(conn) as client:
        for index, source in enumerate(sources):
            if index:
                # Stagger: ten forums hit in the same second is a pattern; the
                # same ten spread over a minute is a visitor.
                time.sleep(config.source_stagger_seconds)

            row = conn.execute(
                "SELECT last_item_at, last_success_at, backfilled_at "
                "FROM sources WHERE id = %s",
                (source.id,),
            ).fetchone()

            # Джерело, увімкнене ПІСЛЯ первинного засіву, історії не має і в
            # звичайному режимі набрати її не може: _watermark() падає на
            # last_success_at, а той оновлюється щопрогону навіть на нулі
            # айтемів, тож вікно назавжди ~72 години. Один прогін у
            # seed-режимі це розмикає. A2 не порушено: seed пише статус
            # "seeded" — історія для дедупу є, в CRM не йде НІЧОГО.
            backfill = row is not None and row["backfilled_at"] is None
            if backfill and not seed:
                log.info("backfilling %s: no history yet, seeding once", source.name)

            try:
                stats = _fetch_source(
                    conn, client, source, row, keywords, seed or backfill
                )
            except SourceBlocked as exc:
                failures.append(f"{source.name}: BLOCKED — {exc}")
                _record_failure(conn, source.id)
                totals["sources_failed"] += 1
                continue
            except (FetchError, ValueError, KeyError) as exc:
                failures.append(f"{source.name}: {type(exc).__name__} — {exc}")
                _record_failure(conn, source.id)
                totals["sources_failed"] += 1
                continue

            totals["sources_checked"] += 1
            totals["items_seen"] += stats["seen"]
            totals["items_new"] += stats["new"]
            totals["items_filtered"] += stats["filtered"]
            totals["items_delivered"] += stats["delivered"]

    if not seed:
        resent, dead = delivery.retry_pending(conn)
        totals["items_delivered"] += resent
        totals["items_dead"] = dead
        _check_dark_sources(conn)

    _prune_items_log(conn)

    conn.execute(
        """
        UPDATE worker_runs
           SET finished_at = now(), sources_checked = %s, sources_failed = %s,
               items_seen = %s, items_new = %s, items_filtered = %s,
               items_delivered = %s, items_dead = %s, detail = %s
         WHERE id = %s
        """,
        (
            totals["sources_checked"], totals["sources_failed"], totals["items_seen"],
            totals["items_new"], totals["items_filtered"], totals["items_delivered"],
            totals["items_dead"], psycopg.types.json.Json({"failures": failures}), run_id,
        ),
    )
    conn.commit()

    if failures:
        alert(
            f"{len(failures)} source(s) failed this run:\n"
            + "\n".join(f"• {f}" for f in failures[:10])
        )

    log.info("run %s finished: %s", run_id, totals)
    return totals


def _record_failure(conn: psycopg.Connection, source_id: int) -> None:
    conn.execute(
        "UPDATE sources SET consecutive_failures = consecutive_failures + 1 WHERE id = %s",
        (source_id,),
    )
    conn.commit()


def run_with_monitoring(conn: psycopg.Connection, *, seed: bool = False) -> dict[str, Any]:
    """run_once plus the dead-man's switch. Silence here means the worker died."""
    ping_healthchecks("/start")
    try:
        totals = run_once(conn, seed=seed)
    except RunAlreadyInProgress:
        # Not a failure: another process holds the lock. Skipping is the correct
        # outcome, and alerting on it would train the team to ignore alerts.
        log.warning("run skipped — another worker run is in progress")
        raise
    except Exception as exc:  # noqa: BLE001 — the run must report before it dies
        ping_healthchecks("/fail")
        alert(f"worker run crashed: {type(exc).__name__} — {exc}", level="error")
        raise
    ping_healthchecks()
    return totals
