#!/usr/bin/env bash
# Nightly backup (A5, Deployment-Plan Stage 1.6).
#
# Losing Postgres without a backup wipes the dedup memory, and the next run
# re-delivers every item the system has ever seen. This is the cheapest
# insurance in the design — and it is only insurance once a restore has been
# tested, so run scripts/restore-test.sh at least once before go-live.
#
# Decision (DEVLOG 2026-08-07, trap #28): git was the wrong transport for
# n8n workflow backups. This script used to `git commit` the nightly export
# straight into n8n/workflows/ on the server; the moment anyone pushed from a
# laptop that history diverged from origin, and every following
# `git pull --ff-only` on the box failed — it bit twice in one week. The
# server must never create local commits. Workflow snapshots now go to a
# plain, untracked, timestamped directory instead (see step 2 below).
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

# 2. n8n workflows + credentials as JSON, point-in-time snapshot.
#    The n8n container only has ./n8n bind-mounted as /backup (docker-compose.yml),
#    so the export has to land inside the repo tree first. It goes to a
#    dot-prefixed staging dir — never n8n/workflows/, that path is git-tracked
#    source, not a backup target — and is moved out immediately to a plain,
#    untracked, timestamped directory. Nothing here ever touches git.
N8N_WF_BACKUP_DIR="${N8N_WF_BACKUP_DIR:-/opt/rfp/backups/n8n-workflows}"
N8N_WF_KEEP="${N8N_WF_KEEP:-14}"
STAGING="n8n/.export-$STAMP"
mkdir -p "$STAGING/workflows"

$COMPOSE exec -T n8n \
    n8n export:workflow --all --separate --output="/backup/.export-$STAMP/workflows" >/dev/null
$COMPOSE exec -T n8n \
    n8n export:credentials --all --output="/backup/.export-$STAMP/credentials.json" >/dev/null

mkdir -p "$N8N_WF_BACKUP_DIR"
mv "$STAGING" "$N8N_WF_BACKUP_DIR/$STAMP"
echo "n8n workflow snapshot: $N8N_WF_BACKUP_DIR/$STAMP"

# Keep only the newest N8N_WF_KEEP snapshot dirs — count-based, mirrors the
# mtime-based prune below in spirit (one run/night, so "newest 14 dirs" and
# "newest 14 days" land on the same set).
ls -1dt "$N8N_WF_BACKUP_DIR"/*/ 2>/dev/null | tail -n +$((N8N_WF_KEEP + 1)) | xargs -r rm -rf --

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
