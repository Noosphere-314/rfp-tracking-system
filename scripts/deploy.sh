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
# Caddy sits behind `profiles: ["prod"]`, so it needs its own pinned invocation.
COMPOSE_PROD="docker compose -f docker-compose.yml --profile prod"

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
echo "=== 3/7  check .env ==="
# The dashboard refuses to start without these (admin/auth.py fails fast rather
# than serving an open panel on a public domain). Combined with
# `restart: unless-stopped`, a missing value would otherwise be an endless
# crash-loop that `docker compose ps` reports as "running".
for v in DASHBOARD_PASSWORD SESSION_SECRET N8N_URL; do
    if ! grep -qE "^${v}=.+" .env; then
        echo "  .env: ${v} is not set — deploy stopped (the dashboard will not start without it)" >&2
        exit 1
    fi
done
echo "  DASHBOARD_PASSWORD, SESSION_SECRET, N8N_URL — present"

echo
echo "=== 4/7  rebuild images ==="
$COMPOSE build --quiet worker admin kbmcp

echo
echo "=== 5/7  migrate ==="
$COMPOSE run --rm worker migrate

echo
echo "=== 6/7  restart services ==="
# `up -d` recreates only what changed. The worker finishes its current run
# first: SIGTERM is handled, and an interrupted run would leave items pending
# rather than lost, but a clean stop keeps the logs readable.
$COMPOSE up -d postgres n8n worker admin kbmcp

# Caddy is prod-profile only, so the plain $COMPOSE above never touched it —
# which meant every Caddyfile change since the domain went up required a manual
# restart nobody documented. Reload rather than restart: it re-reads the config
# without dropping in-flight connections or re-negotiating TLS.
$COMPOSE_PROD up -d caddy
$COMPOSE_PROD exec -T caddy caddy reload --config /etc/caddy/Caddyfile 2>/dev/null \
    || echo "  caddy reload failed — check 'docker compose -f docker-compose.yml --profile prod logs caddy'" >&2

echo
echo "=== 7/7  verify ==="
# Wait for the healthcheck instead of a fixed sleep: `ps` right after `up -d`
# happily shows a container that is about to crash-loop as "running".
status=""
for _ in $(seq 1 20); do
    status="$($COMPOSE ps --format json admin 2>/dev/null \
        | python3 -c 'import sys,json; print(json.loads(sys.stdin.read() or "{}").get("Health",""))' 2>/dev/null || true)"
    [ "$status" = "healthy" ] && break
    sleep 3
done
if [ "$status" != "healthy" ]; then
    echo "  admin did not become healthy — last 50 lines:" >&2
    $COMPOSE logs --tail 50 admin >&2
    exit 1
fi
echo "  admin: healthy"
$COMPOSE ps
echo
$COMPOSE run --rm worker verify

echo
echo "Deployed $AFTER."
echo "Watch the next run:  docker compose -f docker-compose.yml logs -f worker"
