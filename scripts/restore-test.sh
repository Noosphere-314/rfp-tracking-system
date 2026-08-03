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

BACKUP_DIR="${BACKUP_DIR:-/var/backups/rfp}"
DUMP="${1:-$(ls -t "$BACKUP_DIR"/rfp-*.sql.gz 2>/dev/null | head -1)}"
TEST_DB="restore_test_$(date -u +%Y%m%d%H%M%S)"

if [ -z "$DUMP" ] || [ ! -f "$DUMP" ]; then
    echo "no dump found in $BACKUP_DIR — run scripts/backup.sh first" >&2
    exit 1
fi
echo "restoring $DUMP into $TEST_DB"

docker compose exec -T postgres psql -U "$POSTGRES_USER" -d postgres \
    -c "CREATE DATABASE $TEST_DB"

trap 'docker compose exec -T postgres psql -U "$POSTGRES_USER" -d postgres \
        -c "DROP DATABASE IF EXISTS $TEST_DB" >/dev/null' EXIT

gunzip -c "$DUMP" | docker compose exec -T postgres \
    psql -q -U "$POSTGRES_USER" -d "$TEST_DB" >/dev/null

echo "--- restored contents ---"
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$TEST_DB" -c "
    SELECT 'sources'    AS table, count(*) FROM sources
    UNION ALL SELECT 'keywords',   count(*) FROM keywords
    UNION ALL SELECT 'settings',   count(*) FROM settings
    UNION ALL SELECT 'seen_items', count(*) FROM seen_items
    UNION ALL SELECT 'org_registry', count(*) FROM org_registry;"

echo
echo "Restore verified. Confirm the counts match production before trusting this backup."
