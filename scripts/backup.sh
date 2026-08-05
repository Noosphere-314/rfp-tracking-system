#!/usr/bin/env bash
# Nightly backup (A5, Deployment-Plan Stage 1.6).
#
# Losing Postgres without a backup wipes the dedup memory, and the next run
# re-delivers every item the system has ever seen. This is the cheapest
# insurance in the design — and it is only insurance once a restore has been
# tested, so run scripts/restore-test.sh at least once before go-live.
#
# Cron (on the host):  15 3 * * *  /opt/rfp/scripts/backup.sh >> /var/log/rfp-backup.log 2>&1
set -euo pipefail

cd "$(dirname "$0")/.."
set -a && source .env && set +a

# Always pin the base file. docker-compose.override.yml ships in the repo, so it
# is present on the server too — an unpinned `docker compose` here would target
# the dev stack instead of production.
COMPOSE="docker compose -f docker-compose.yml"

BACKUP_DIR="${BACKUP_DIR:-/var/backups/rfp}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP_DIR"

# 1. Database — schema and data, all schemas including n8n's.
#    KB archive bodies (kb.topics/kb.posts) are excluded: they are fully
#    reproducible by `worker kb-backfill` and would multiply the dump size.
#    kb.forums (crawl state) and kb.query_log stay in (KB-Module-Design §11).
DUMP="$BACKUP_DIR/rfp-$STAMP.sql.gz"
$COMPOSE exec -T postgres \
    pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists \
    --exclude-table-data='kb.topics' --exclude-table-data='kb.posts' \
    | gzip > "$DUMP"
echo "database dump: $DUMP ($(du -h "$DUMP" | cut -f1))"

# 2. n8n workflows as JSON. This doubles as version history: the export is
#    committed to git, which is the closest thing CE has to workflow diffs.
$COMPOSE exec -T n8n \
    n8n export:workflow --all --separate --output=/backup/workflows >/dev/null
$COMPOSE exec -T n8n \
    n8n export:credentials --all --output=/backup/credentials.json >/dev/null

if [ -d .git ]; then
    git add n8n/workflows >/dev/null 2>&1 || true
    git -c user.email=backup@localhost -c user.name="backup" \
        commit -q -m "n8n workflow export $STAMP" n8n/workflows 2>/dev/null || true
fi

# 3. Off-box copy. Restoring from a backup that lived on the box that died is
#    not restoring.
if [ -n "${B2_BUCKET:-}" ]; then
    rclone copy "$DUMP" "b2:$B2_BUCKET/postgres/"
elif [ -n "${STORAGE_BOX_TARGET:-}" ]; then
    rsync -a "$DUMP" "$STORAGE_BOX_TARGET/postgres/"
else
    echo "WARNING: no off-box destination configured (B2_BUCKET or STORAGE_BOX_TARGET)" >&2
fi

find "$BACKUP_DIR" -name 'rfp-*.sql.gz' -mtime +"$KEEP_DAYS" -delete
echo "backup complete"
