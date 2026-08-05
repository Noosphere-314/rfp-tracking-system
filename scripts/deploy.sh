#!/usr/bin/env bash
# Deploy the current main branch onto this box.
#
# Deliberately manual: CI does not push to production. Migrations are
# irreversible, and a bad source config surfaces hours later as missing leads —
# neither belongs behind an automatic trigger on `git push`.
#
# Usage (on the server):  /opt/rfp/scripts/deploy.sh
set -euo pipefail

cd "$(dirname "$0")/.."

# The dev override ships in the repo and would silently redirect deliveries to
# the local mock. Every compose call here is pinned to the base file.
COMPOSE="docker compose -f docker-compose.yml"

echo "=== 1/6  backup before touching anything ==="
# A migration that goes wrong is exactly when you want last night's dump to be
# this morning's dump.
if ! scripts/backup.sh; then
    echo "backup failed — refusing to deploy" >&2
    exit 1
fi

echo
echo "=== 2/6  fetch ==="
BEFORE="$(git rev-parse --short HEAD)"
git fetch --quiet origin
git checkout --quiet main
git pull --quiet --ff-only origin main
AFTER="$(git rev-parse --short HEAD)"

if [ "$BEFORE" = "$AFTER" ]; then
    echo "already at $AFTER — nothing new to deploy"
else
    echo "$BEFORE → $AFTER"
    git --no-pager log --oneline "$BEFORE..$AFTER" | sed 's/^/  /'
fi

echo
echo "=== 3/6  rebuild images ==="
$COMPOSE build --quiet worker admin kbmcp

echo
echo "=== 4/6  migrate ==="
$COMPOSE run --rm worker migrate

echo
echo "=== 5/6  restart services ==="
# `up -d` recreates only what changed. The worker finishes its current run
# first: SIGTERM is handled, and an interrupted run would leave items pending
# rather than lost, but a clean stop keeps the logs readable.
$COMPOSE up -d postgres n8n worker admin kbmcp

echo
echo "=== 6/6  verify ==="
sleep 5
$COMPOSE ps
echo
$COMPOSE run --rm worker verify

echo
echo "Deployed $AFTER."
echo "Watch the next run:  docker compose -f docker-compose.yml logs -f worker"
