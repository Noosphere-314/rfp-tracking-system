#!/usr/bin/env bash
# Restore rehearsal (Deployment-Plan Stage 1.7, exit criterion).
#
# Restores the newest dump into a throwaway database beside the live one and
# reports row counts. It never touches the production database — an untested
# backup is a guess, and a restore test that could destroy the thing it is
# protecting would not get run.
set -euo pipefail

cd "$(dirname "$0")/.."
set -a && source .env && set +a

# See backup.sh: the dev override ships in the repo, so compose must be pinned.
COMPOSE="docker compose -f docker-compose.yml"

BACKUP_DIR="${BACKUP_DIR:-/var/backups/rfp}"
DUMP="${1:-$(ls -t "$BACKUP_DIR"/rfp-*.sql.gz 2>/dev/null | head -1)}"
TEST_DB="restore_test_$(date -u +%Y%m%d%H%M%S)"

if [ -z "$DUMP" ] || [ ! -f "$DUMP" ]; then
    echo "no dump found in $BACKUP_DIR — run scripts/backup.sh first" >&2
    exit 1
fi
echo "restoring $DUMP into $TEST_DB"

$COMPOSE exec -T postgres psql -U "$POSTGRES_USER" -d postgres \
    -c "CREATE DATABASE $TEST_DB"

trap '$COMPOSE exec -T postgres psql -U "$POSTGRES_USER" -d postgres \
        -c "DROP DATABASE IF EXISTS $TEST_DB" >/dev/null' EXIT

gunzip -c "$DUMP" | $COMPOSE exec -T postgres \
    psql -q -U "$POSTGRES_USER" -d "$TEST_DB" >/dev/null

# 2026-08-31: the check used to stop at five public tables while the dump
# also carries kb.* (briefs, chat history, crawl state, deadlines) and n8n.*
# (workflows) — a restore could silently lose those and still "pass". The
# same counting query now runs against BOTH the restored copy and the live
# database, side by side; the live side is read-only. kb.topics/kb.posts are
# excluded from the dump on purpose (reproducible via kb-backfill), so they
# are deliberately absent here.
COUNTS_SQL="
    SELECT 'sources'          AS table, count(*) FROM sources
    UNION ALL SELECT 'keywords',        count(*) FROM keywords
    UNION ALL SELECT 'settings',        count(*) FROM settings
    UNION ALL SELECT 'seen_items',      count(*) FROM seen_items
    UNION ALL SELECT 'items_log',       count(*) FROM items_log
    UNION ALL SELECT 'org_registry',    count(*) FROM org_registry
    UNION ALL SELECT 'kb.forums',       count(*) FROM kb.forums
    UNION ALL SELECT 'kb.briefs',       count(*) FROM kb.briefs
    UNION ALL SELECT 'kb.chat_messages',count(*) FROM kb.chat_messages
    UNION ALL SELECT 'kb.deadlines',    count(*) FROM kb.deadlines
    UNION ALL SELECT 'n8n.workflows',   count(*) FROM n8n.workflow_entity
    ORDER BY 1;"

echo "--- restored copy ---"
$COMPOSE exec -T postgres psql -U "$POSTGRES_USER" -d "$TEST_DB" -c "$COUNTS_SQL"

echo "--- live database (for comparison; a nightly dump lags by design) ---"
$COMPOSE exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$COUNTS_SQL"

echo
echo "Restore verified. The two tables above should differ only by activity"
echo "since the dump was taken; a zero on the restored side that is non-zero"
echo "live means that table is NOT covered by the backup."
