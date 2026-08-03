"""Worker entrypoint.

    migrate      apply pending SQL migrations
    run          one pass over every enabled source
    loop         run, sleep, repeat (the container's default command)
    seed         A2 seed run: record all visible history, deliver nothing
    sources      list configured sources and their health
    verify       check config and connectivity without fetching anything

    kb-backfill  archive enabled KB forums completely (resumable; overnight job)
    kb-update    incremental KB refresh: new posts + bumped topics
    kb-status    KB archive statistics per forum
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from types import FrameType

from . import pipeline
from .config import config
from .db import apply_migrations, connect

log = logging.getLogger("worker")

_stopping = False


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    """Finish the current run, then exit — never abandon a run mid-source."""
    global _stopping
    _stopping = True
    log.info("received signal %s; will stop after the current run", signum)


def cmd_migrate(_args: argparse.Namespace) -> int:
    applied = apply_migrations()
    print(f"applied {len(applied)} migration(s): {', '.join(applied) or 'none pending'}")
    return 0


def cmd_run(_args: argparse.Namespace) -> int:
    config.validate()
    with connect() as conn:
        try:
            pipeline.run_with_monitoring(conn)
        except pipeline.RunAlreadyInProgress as exc:
            print(f"skipped: {exc}")
            return 2
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    """Mark everything currently visible as seen, without delivering any of it.

    Deployment-Plan sequencing rule 3: never go live without this. It is the
    difference between a working system and a CRM flooded with stale leads.
    """
    config.validate()
    if not args.yes:
        answer = input(
            "Seed run: every currently visible item will be recorded as 'seeded' "
            "and never delivered. Continue? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1

    with connect() as conn:
        totals = pipeline.run_once(conn, seed=True)
    print(f"seeded {totals['items_new']} item(s) across {totals['sources_checked']} source(s)")
    return 0


def cmd_loop(_args: argparse.Namespace) -> int:
    config.validate()
    # The container is the only thing that reliably runs at deploy time, so this
    # is where schema changes land. Migrations are idempotent by filename.
    applied = apply_migrations()
    if applied:
        log.info("applied migrations: %s", ", ".join(applied))

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("worker loop started; interval %ds", config.run_interval_seconds)
    while not _stopping:
        try:
            with connect() as conn:
                pipeline.run_with_monitoring(conn)
        except Exception:  # noqa: BLE001
            # Already alerted in run_with_monitoring. A crashed run must not end
            # the loop: the next hour's sources may be perfectly fine, and the
            # pending items are retried anyway.
            log.exception("run failed; continuing to next interval")

        slept = 0
        while slept < config.run_interval_seconds and not _stopping:
            time.sleep(min(5, config.run_interval_seconds - slept))
            slept += 5

    log.info("worker loop stopped")
    return 0


def cmd_sources(_args: argparse.Namespace) -> int:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.type, s.name, s.ecosystem, s.enabled, s.quarantined,
                   s.lane, s.last_success_at, s.last_item_at, s.consecutive_failures,
                   (SELECT count(*) FROM seen_items i WHERE i.source_id = s.id) AS items
              FROM sources s ORDER BY s.enabled DESC, s.type, s.name
            """
        ).fetchall()

    for row in rows:
        flag = "✗ off" if not row["enabled"] else ("⚠ quarantined" if row["quarantined"] else "✓ on")
        last = row["last_item_at"].strftime("%Y-%m-%d") if row["last_item_at"] else "never"
        print(
            f"{row['id']:>3}  {flag:<14} {row['type']:<10} {row['lane']:<8} "
            f"{row['name']:<28} items={row['items']:<6} last_item={last} "
            f"fails={row['consecutive_failures']}"
        )
    return 0


def cmd_verify(_args: argparse.Namespace) -> int:
    """Pre-flight: config present, database reachable, migrations applied."""
    problems: list[str] = []
    try:
        config.validate()
    except RuntimeError as exc:
        problems.append(str(exc))

    if not config.slack_webhook_url:
        problems.append("SLACK_WEBHOOK_URL is unset — worker alerts go nowhere")
    if not config.healthchecks_url:
        problems.append("HEALTHCHECKS_URL is unset — no dead-man's switch")
    if not config.snapshot_api_key:
        problems.append("SNAPSHOT_API_KEY is unset — Snapshot runs on keyless limits")

    try:
        with connect() as conn:
            pending = conn.execute(
                "SELECT count(*) AS n FROM seen_items WHERE status = 'pending'"
            ).fetchone()["n"]
            sources = conn.execute(
                "SELECT count(*) AS n FROM sources WHERE enabled"
            ).fetchone()["n"]
            print(f"database OK — {sources} enabled source(s), {pending} pending item(s)")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"database unreachable or unmigrated: {exc}")

    for problem in problems:
        print(f"  ! {problem}")
    return 1 if any("missing required" in p or "unreachable" in p for p in problems) else 0


def cmd_kb_backfill(args: argparse.Namespace) -> int:
    """Resumable full archive. No config.validate(): the KB needs only the DB."""
    from . import kb
    from .http import HttpClient, SourceBlocked

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    with connect() as conn:
        forums = kb.load_forums(conn, args.forum)
        if not forums:
            print(f"no enabled KB forum matches {args.forum!r}" if args.forum
                  else "no enabled KB forums — check kb.forums")
            return 1
        with HttpClient(conn) as client:
            for forum in forums:
                if _stopping:
                    break
                if forum.backfill_done and not args.again:
                    print(f"{forum.forum_slug}: backfill already done (use --again to re-walk)")
                    continue
                try:
                    stats = kb.backfill(
                        conn, client, forum,
                        max_topics=args.max_topics,
                        should_stop=lambda: _stopping,
                    )
                    print(f"{forum.forum_slug}: {stats}")
                except SourceBlocked as exc:
                    kb.record_failure(conn, forum, exc)
                    print(f"{forum.forum_slug}: BLOCKED (403/429) — stopping this forum: {exc}")
                except Exception as exc:  # noqa: BLE001 — cursor is saved, resume is safe
                    kb.record_failure(conn, forum, exc)
                    print(f"{forum.forum_slug}: failed ({exc}); cursor saved, re-run to resume")
    return 0


def cmd_kb_update(args: argparse.Namespace) -> int:
    from . import kb
    from .http import HttpClient

    with connect() as conn:
        forums = kb.load_forums(conn, args.forum)
        with HttpClient(conn) as client:
            for forum in forums:
                if not forum.backfill_done:
                    print(f"{forum.forum_slug}: skipped — backfill not finished yet")
                    continue
                try:
                    stats = kb.incremental(conn, client, forum)
                    print(f"{forum.forum_slug}: {stats}")
                except Exception as exc:  # noqa: BLE001
                    kb.record_failure(conn, forum, exc)
    return 0


def cmd_kb_status(_args: argparse.Namespace) -> int:
    from . import kb

    with connect() as conn:
        for row in kb.status(conn):
            state = "✓ done" if row["backfill_done"] else ("… crawling" if row["enabled"] else "✗ off")
            newest = row["newest_activity"].strftime("%Y-%m-%d") if row["newest_activity"] else "—"
            print(
                f"{row['forum_slug']:<12} {state:<11} topics={row['topics']:<7} "
                f"posts={row['posts']:<8} newest={newest} fails={row['consecutive_failures']}"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(prog="worker", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate").set_defaults(func=cmd_migrate)
    sub.add_parser("run").set_defaults(func=cmd_run)
    sub.add_parser("loop").set_defaults(func=cmd_loop)
    sub.add_parser("sources").set_defaults(func=cmd_sources)
    sub.add_parser("verify").set_defaults(func=cmd_verify)

    seed = sub.add_parser("seed")
    seed.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    seed.set_defaults(func=cmd_seed)

    kb_backfill = sub.add_parser("kb-backfill")
    kb_backfill.add_argument("--forum", help="single forum slug (default: all enabled)")
    kb_backfill.add_argument("--max-topics", type=int, help="stop after N topics (smoke test)")
    kb_backfill.add_argument("--again", action="store_true", help="re-walk a finished backfill")
    kb_backfill.set_defaults(func=cmd_kb_backfill)

    kb_update = sub.add_parser("kb-update")
    kb_update.add_argument("--forum", help="single forum slug (default: all enabled)")
    kb_update.set_defaults(func=cmd_kb_update)

    sub.add_parser("kb-status").set_defaults(func=cmd_kb_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
