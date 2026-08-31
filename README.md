# Web3 RFP & Signal Tracking System

Automated discovery of Web3 RFPs, grants, and capital events across governance
forums, Snapshot, aggregator APIs, and news feeds — delivered as enriched lead
cards in **Pipedrive**, each one carrying a briefing pack researched from a
full-text archive of the ecosystem's governance history.

Two subsystems share one database:

| Subsystem | Question it answers | How |
|---|---|---|
| **Pipeline** | *What is new?* | Hourly fetch → dedup → regex prefilter → Claude classification → Pipedrive lead |
| **Knowledge base** | *What do we know?* | Full archive of governance forums + Postgres FTS, queried by Claude over MCP |

The two meet in the **briefing pack**: when the pipeline creates a lead, the
knowledge base is asked what this ecosystem has funded before, who decides, and
what got rejected — and the answer is attached to the lead as a cited note.

---

## Architecture

```
  Sources (Discourse · Snapshot · RSS · REST · DefiLlama · GitHub)
        │
        ▼
  ┌──────────────────────────────────────────────┐
  │  worker (Python)                             │
  │  fetch → dedup (3 identities) → prefilter    │
  │  → deliver (at-least-once) → retry/dead      │
  │  + kb-crawl mode (forum archiver)            │
  └──────────────────────────────────────────────┘
        │ webhook                        │
        ▼                                ▼
  ┌───────────────┐              ┌──────────────────┐
  │  n8n          │              │  Postgres        │
  │  classify     │◄────────────►│  public.* (leads)│
  │  → enrich     │              │  kb.*    (archive)│
  │  → Pipedrive  │              │  n8n.*   (flows) │
  │  → brief note │              └──────────────────┘
  └───────────────┘                       ▲
        │                                 │
        ▼                        ┌────────────────────┐
   Pipedrive Leads Inbox         │  kbmcp (FastMCP)   │
   + Slack alerts/digest         │  search / topic /  │
                                 │  brief generator   │
                                 └────────────────────┘
                                          ▲
                                   Claude Code / Desktop
```

**Design split:** the worker is deterministic (fetching, deduplication, delivery
state) and owns correctness; n8n is the visual layer (classification prompts,
CRM mapping, alerting) and owns team-editable business logic. Neither can
silently break the other's guarantees.

### Services

| Service | Port (local) | Purpose |
|---|---|---|
| `postgres` | `127.0.0.1:54329` | All state: leads, sources, archive, n8n workflows |
| `worker` | — | Fetch/dedup/deliver loop + KB crawler (CLI) |
| `n8n` | `127.0.0.1:5678` | Classification → Pipedrive → alerts pipeline |
| `admin` | `127.0.0.1:8080` | Engineer dashboard: sources, keywords, thresholds, items, KB |
| `kbmcp` | `127.0.0.1:8765` | MCP server over the forum archive + briefing packs |
| `caddy` | `80/443` | **Production only** (profile `prod`): TLS + public gateway |

---

## Requirements

- Docker Engine 24+ with Compose v2 (older engines cannot unpack recent images)
- **8 GB RAM**, ~20 GB disk. 4 GB runs the pipeline alone, but n8n takes roughly a
  gigabyte on its own and the knowledge base adds GIN indexes on top — two archived
  forums already occupy 249 MB. On Hetzner that means CX32, not CX22.
- Outbound HTTPS

No API keys are required to run the pipeline against live sources. Keys unlock
individual capabilities (see [Configuration](#configuration)).

---

## Quick start (local)

```bash
git clone <this-repo> && cd RFP-Tracking-System
cp .env.example .env
```

Generate secrets and fill them into `.env` (`POSTGRES_PASSWORD`,
`N8N_DB_PASSWORD`, `N8N_ENCRYPTION_KEY`, `N8N_WEBHOOK_SECRET`, `KB_MCP_TOKEN`):

```bash
python3 -c "import secrets; [print(f'{k}={secrets.token_hex(24)}') for k in ('POSTGRES_PASSWORD','N8N_DB_PASSWORD','N8N_ENCRYPTION_KEY','N8N_WEBHOOK_SECRET','KB_MCP_TOKEN')]"
```

Build the images:

```bash
docker compose build
```

Start the stack:

```bash
docker compose up -d postgres admin n8n kbmcp
```

Apply database migrations (creates schema + seed configuration):

```bash
docker compose run --rm worker migrate
```

Run one full pass over every enabled source:

```bash
docker compose run --rm worker run
```

Open the dashboards:

- **Admin** — <http://localhost:8080>
- **n8n editor** — <http://localhost:5678>
- **KB health** — <http://localhost:8765/health>

For local development, create the override once (it is gitignored so it can
never reach the server, where plain `docker compose up` would auto-load it):

```bash
cp docker-compose.override.yml.example docker-compose.override.yml
```

With it in place `docker compose up` applies it automatically:
deliveries go to a **mock pipeline** inside the admin service, so the full
`pending → done` cycle works with no Pipedrive, no Claude, and no n8n credentials.

To run the worker continuously instead of one pass at a time:

```bash
docker compose up -d worker
```

---

## Worker CLI

Every command runs as `docker compose run --rm worker <command>`.

| Command | Description |
|---|---|
| `migrate` | Apply pending SQL migrations (idempotent, by filename) |
| `run` | One pass: fetch → dedup → filter → deliver → retry pending |
| `loop` | `run` + sleep, forever (the container's default command) |
| `seed` | Record all currently visible history as `seeded`, deliver nothing |
| `sources` | List configured sources and their health |
| `verify` | Pre-flight: config present, database reachable, migrations applied |
| `kb-backfill` | Archive enabled forums completely (resumable; overnight job) |
| `kb-update` | Incremental KB refresh: new posts + bumped topics |
| `kb-status` | Archive statistics per forum |

`kb-backfill` accepts `--forum <slug>`, `--max-topics N` (smoke test), and
`--again` (re-walk a finished backfill).

---

## Configuration

All operational tuning lives in the database and is editable from the admin UI
**without a redeploy**. Only secrets and infrastructure live in `.env`.

### `.env` — secrets and infrastructure

| Variable | Required | Purpose |
|---|:---:|---|
| `POSTGRES_PASSWORD`, `N8N_DB_PASSWORD` | ✅ | Database credentials |
| `N8N_ENCRYPTION_KEY` | ✅ | n8n credential encryption — **losing it invalidates every stored credential** |
| `N8N_WEBHOOK_SECRET` | ✅ | Shared secret between worker and the n8n webhook |
| `KB_MCP_TOKEN` | ✅ | Bearer token for the KB MCP server and briefing endpoint |
| `DOMAIN`, `ACME_EMAIL` | prod | TLS certificate issuance via Caddy |
| `ANTHROPIC_API_KEY` | — | Enables analyst-grade briefing packs (keyword tier without it) |
| `SNAPSHOT_API_KEY` | — | Higher Snapshot rate limits (approval takes ~72h — request early) |
| `GITHUB_TOKEN` | — | Enables the `github_discussions` fetcher (free read-only PAT) |
| `SLACK_WEBHOOK_URL`, `HEALTHCHECKS_URL` | — | Worker alerts and dead-man's switch |
| `USER_AGENT` | — | Sent to every source — use a real contact address |

Rate limiting, lookback windows, retry counts, and timeouts also live in `.env`
with sensible defaults; see `.env.example`.

### Database settings (admin UI → Settings)

| Key | Default | Meaning |
|---|---|---|
| `confidence_threshold` | `0.7` | At or above → lead is created automatically |
| `review_band_low` | `0.4` | Between the two → routed to Slack for human review |
| `max_leads_per_run` | `25` | Volume guard protecting inbox trust |
| `lead_floor_7d` | `3` | Fewer leads than this in 7 days raises an alarm |
| `source_dark_days` | `14` | A silent source this long raises an alarm |
| `brief_model` | `claude-opus-5` | Model used for briefing packs |
| `brief_language` | `en` | Briefing pack output language |

---

## Sources

A source is a row in the `sources` table. **Adding one requires no code and no
deploy** — the admin UI runs a live test-fetch before saving, so a source that
cannot produce items is never stored as enabled.

Six fetcher types are implemented:

| Type | Covers | Config shape |
|---|---|---|
| `discourse` | Any Discourse forum (most DAO governance forums) | `{"categories": [{"slug": "grants", "id": 12}]}` |
| `snapshot` | Snapshot proposals via GraphQL | `{"spaces": ["ens.eth", "safe.eth"]}` |
| `rss` | Any RSS/Atom feed | `{}` |
| `rest_aggregator` | **Any JSON REST API** — field mapping in config | `items_path` + `fields` + optional `require`, `headers`, `url_base` |
| `defillama` | TVL spikes as a funding signal | `{"min_tvl": 1000000, "min_change_7d": 50}` |
| `github_discussions` | GitHub Discussions via GraphQL | `{"repos": [{"owner": "org", "name": "repo"}]}` |

`rest_aggregator` is the extension point. To connect a new API, describe where
the items live and what the fields mean:

```json
{
  "headers": {"X-Api-Key": "$CRYPTORANK_API_KEY"},
  "params": {"limit": "100"},
  "items_path": "data",
  "fields": {
    "external_id": "id",
    "title": "name",
    "url": "links.website",
    "body": "description",
    "ts": "date"
  },
  "require": {"isOpen": true}
}
```

A `$NAME` header value is resolved from the worker's environment — the secret
stays in `.env` and never touches the database or the UI.

### Verifying a new Discourse forum before adding it

```bash
UA="RFP-Tracker/1.0 (+mailto:you@example.com)"
curl -sA "$UA" https://forum.example.org/categories.json \
  | python3 -c "import json,sys; [print(c['id'], c['slug']) for c in json.load(sys.stdin)['category_list']['categories']]"
curl -sA "$UA" -o /dev/null -w "%{http_code}\n" https://forum.example.org/c/grants/12.json
```

`200` means it can be connected. `403` means Cloudflare is blocking datacenter
IPs — that source needs an egress proxy and should not be retried aggressively.

---

## Delivery guarantees

Three properties the worker enforces, and which must not be weakened:

**At-least-once delivery.** An item becomes `done` only when the *final* node of
the n8n workflow writes that status — after the Pipedrive lead exists. A `200`
from the webhook proves receipt, not delivery. Until `done` is written the worker
re-sends the item (up to `MAX_DELIVERY_ATTEMPTS`, then `dead` + alert). Never
move the status write earlier in the workflow.

**Seed before go-live.** `worker seed` records everything currently visible as
`seeded` — permanently deduplicated, never delivered. Skipping this floods the
CRM with hundreds of stale leads on day one.

**Three identities per item.** `item_uid` (primary-key dedup),
`content_hash` (change detection), and `fingerprint` — a normalized bag of words
from the title with ecosystem names removed, so the same RFP posted to a forum
and to Snapshot collapses into one lead.

Item states: `pending` → `done` · `filtered` (failed the keyword prefilter) ·
`seeded` (pre-launch history) · `dead` (delivery exhausted).

---

## Knowledge base (research mode)

The archiver walks whole governance forums into `kb.topics` / `kb.posts` with
Postgres full-text indexes, then exposes them to Claude over MCP. Governance
forums are small — roughly 25–30k topic fetches for ten of them, under 1 GB of
text, one overnight crawl at a polite ~1 req/s shared with the pipeline through a
common token bucket.

Only robots.txt-permitted JSON endpoints are used (`/categories.json`,
`/c/<slug>/<id>.json`, `/t/<id>.json`, `/posts.json`, `/latest.json`). Search
endpoints and RSS are never used for crawling.

Register a forum in the admin UI (**KB → Register forum**), then:

```bash
docker compose run -d --rm --name kb-backfill worker kb-backfill
```

The crawl is resumable — interrupt it freely; the cursor advances per listing
page and per topic. Keep it fresh with `kb-update` (cheap: two requests plus
whatever changed) on an hourly or daily cron.

Connect it to Claude Code:

```bash
claude mcp add rfp-kb --transport http http://localhost:8765/mcp --header "Authorization: Bearer $(grep KB_MCP_TOKEN .env | cut -d= -f2)"
```

Then ask questions in plain language — *"What has Optimism funded for developer
tooling, and who approved it?"* — and Claude will search, read whole threads, and
answer with links to specific posts. Tools exposed: `search_kb`, `get_topic`,
`live_forum_search`, `brief_for_lead`.

Every query is logged to `kb.query_log`, which is the evidence base for deciding
whether semantic search is ever needed. The schema is already vector-ready
(post-level granularity, raw text retained) so that upgrade is a task, not a
redesign.

---

## Briefing packs

When a lead is created, the pipeline asks the knowledge base for context and
attaches it to the lead as a note. Two tiers:

- **keyword** (no API key) — similar threads, most active voices, rejection
  discussions, straight from SQL over the archive. Free.
- **analyst** (`ANTHROPIC_API_KEY` set) — Claude runs a bounded tool-use loop
  over `kb_search` / `kb_topic`, investigates like a researcher, and writes a
  short brief where **every claim carries a link to the post that supports it**.
  Uncited claims are dropped rather than guessed.

Briefing generation runs **after** the lead is marked `done`, with
continue-on-fail: a knowledge-base outage can never affect lead delivery.

Trigger one manually from the admin UI (**Items → brief**), or via HTTP:

```bash
curl -X POST http://localhost:8765/brief \
  -H "Authorization: Bearer $KB_MCP_TOKEN" -H "Content-Type: application/json" \
  -d '{"ecosystem": "Optimism", "title": "Governance analytics dashboard"}'
```

---

## n8n workflows

Three importable workflows live in [`n8n/workflows/`](n8n/workflows):

| Workflow | Role |
|---|---|
| `rfp-main` | Webhook → secret check → idempotency → classify → threshold gate → enrich → Pipedrive org/lead/note → Slack → **mark done** → briefing pack |
| `rfp-errors` | Error trigger → Slack alert (items stay `pending` and are re-sent) |
| `rfp-digest` | Daily 09:00 health digest + lead-floor heartbeat |

Import them in the editor (Workflows → Import from File), then create
credentials with these exact names so the nodes bind automatically:

| Credential | Type | Notes |
|---|---|---|
| `rfp-app-db (n8n role)` | Postgres | host `postgres`, database `rfp`, user/password from `.env` |
| `anthropic-api-key (x-api-key)` | Header Auth | header name `x-api-key` |
| `pipedrive-api-token` | Query Auth | parameter `api_token` |
| `slack-bot` | Slack API | bot token |

Pipedrive API note: organizations must use **API v2** (v1 organizations are
retired); leads and notes remain v1, which has no v2 equivalent.

Workflows can also be imported from the command line:

```bash
docker compose exec n8n n8n import:workflow --separate --input=/backup/workflows
```

---

## Production deployment

Production **must** pass `-f docker-compose.yml` explicitly. Without it the dev
override is applied and deliveries silently go to the local mock instead of n8n.

```bash
docker compose -f docker-compose.yml --profile prod up -d
```

The `prod` profile enables Caddy, which terminates TLS and acts as the security
gateway: only `/webhook/*` and `/form/*` are public. The n8n editor, the admin
dashboard, and the MCP server bind to loopback and are reached over Tailscale or
an SSH tunnel.

Deployment order:

1. Provision the host, point a DNS A record at it, install Docker
2. Clone the repository, create `.env` with **production** secrets
   (`MOCK_N8N=false`, real `DOMAIN` and `ACME_EMAIL`)
3. `docker compose -f docker-compose.yml --profile prod up -d`
4. `docker compose -f docker-compose.yml run --rm worker migrate`
5. **`docker compose -f docker-compose.yml run --rm worker seed`** — mandatory
6. Import the n8n workflows, create credentials, add Pipedrive custom fields
7. Enable the workflows, then start the worker loop
8. Schedule `scripts/backup.sh` and run `scripts/restore-test.sh` once

A backup is only insurance after a restore has been tested. `backup.sh` dumps
the database (excluding the reproducible KB archive bodies), exports n8n
workflows as JSON, and copies off-box.

---

## Repository layout

```
docker-compose.yml           postgres · n8n · caddy(prod) · worker · admin · kbmcp
docker-compose.override.yml.example  local dev template: mock delivery, HTTP n8n, exposed ports
migrations/                  001 schema · 002 n8n grants · 003 seed · 004 kb · 005 briefs
worker/                      fetchers, dedup, delivery state machine, KB crawler, CLI
  ├── fetchers/              discourse · snapshot · rss · rest_aggregator · defillama · github
  ├── items.py               the three identities (uid / content hash / fingerprint)
  ├── delivery.py            at-least-once delivery, retry, dead-lettering
  ├── ratelimit.py           per-host token bucket shared with the KB crawler
  └── kb.py                  resumable forum archiver
admin/                       FastAPI dashboard (engineers only) + mock n8n endpoint
mcp/                         FastMCP server: archive search + briefing generator
n8n/workflows/               importable workflow definitions (also a version history)
scripts/                     backup.sh, restore-test.sh
```

---

## Troubleshooting

**Items stuck in `pending`.** The workflow is not reaching its final node. Check
n8n executions; the items are safe and will be re-sent.

**A source shows `quarantined`.** Its configuration failed validation at read
time. The reason is in the tooltip on the admin dashboard; fix the config and
re-enable.

**`403` from a forum.** Cloudflare is blocking the datacenter IP. This is a
known risk for Discourse sources and needs an egress proxy, not a retry loop.

**Cache never hits on the classifier.** The system prompt must exceed the model's
minimum cacheable prefix, otherwise caching silently does nothing. Verify with
`usage.cache_read_input_tokens` in the response.

**`invalid tar header` on `docker pull`.** The Docker engine is too old to unpack
zstd-compressed layers. Upgrade Docker, or pin an older image tag.

**Empty briefing packs.** The ecosystem has no archived forum. Register it under
**KB → Register forum** and run `kb-backfill`.
