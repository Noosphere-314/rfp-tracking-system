# n8n workflows

**Скелети для імпорту лежать у `workflows/`** (`rfp-main.json`, `rfp-errors.json`,
`rfp-digest.json`): n8n editor → Workflows → Import from File. Після імпорту
створити credentials з тими самими іменами, що в нодах:

| Credential | Тип | Хто тримає ключ |
|---|---|---|
| `rfp-app-db (n8n role)` | Postgres | host `postgres`, db `rfp`, user/pass = `N8N_DB_USER`/`N8N_DB_PASSWORD` |
| `anthropic-api-key (x-api-key)` | Header Auth | header `x-api-key`, значення — ключ Anthropic |
| `pipedrive-api-token` | Query Auth | param `api_token` |
| `slack-bot` | Slack API | bot token |

Ноди з `TODO` у notes — місця, які добудовуються в редакторі при Stage-2
інтеграції (structured output классифікатора, itemSearch-ідемпотентність,
custom fields лідів, review-band гілка). Каркас і порядок нодів — правильні
і мінятись не повинні (особливо фінальний `mark done`).


This directory is mounted into the n8n container at `/backup`. The nightly
`scripts/backup.sh` writes `n8n export:workflow --all --separate` into
`workflows/` and commits it — that export **is** the workflow version history,
since n8n Community Edition has no git integration.

Credentials are exported too (encrypted, keyed by `N8N_ENCRYPTION_KEY`) but are
gitignored. Losing that key makes them unreadable, so it belongs in the same
place as the other break-glass secrets — not only in `.env` on the box.

## Workflows to build in Stage 2

| Workflow | Trigger | Purpose |
|---|---|---|
| `rfp-main` | Webhook `POST /webhook/rfp-item` | The pipeline. Final node writes `status='done'` — see below. |
| `rfp-errors` | Error Trigger | Any failure in any workflow → Slack (A3). |
| `rfp-digest` | Cron, daily | Sources checked / items new / leads created / errors → Slack. |

## `rfp-main` node order

The order is load-bearing, not stylistic:

1. **Webhook** — reject unless `X-Webhook-Secret` matches. Unsigned POSTs are how
   someone else would burn the Claude budget or inject fake leads.
2. **Idempotency** — Pipedrive search by the Item UID custom field. Already
   present → mark `done` and stop. Fingerprint matches an open lead → append a
   Note and stop (A6).
3. **Classify** — Claude Haiku 4.5, structured output
   (`{is_rfp, confidence, category, canonical_project, canonical_title, reason}`).
   The system prompt must be padded past **4096 tokens** with few-shot examples
   or `cache_control` silently does nothing on Haiku (A9); verify with
   `usage.cache_read_input_tokens`.
4. **Threshold gate** — `settings.confidence_threshold` to auto-deliver;
   `review_band_low`–threshold goes to the review channel instead (A8). Read the
   values from Postgres per run so the form can change them without a redeploy.
5. **Enrich** — Claude Sonnet, 2-sentence description. Deadline and budget only
   when regex-corroborated in the source text, otherwise "not stated".
6. **Organisation** — `org_registry` upsert first, then `POST /api/v2/organizations`
   on a miss. **v2, not v1**: v1 organisation endpoints sunset 2026-07-31.
   Bot-created orgs carry the marker field; never attach a lead to an unmarked
   org (A7).
7. **Lead** — `POST /v1/leads` (v1 is correct here; no v2 exists for leads).
8. **Note** — source link, excerpt, corroborated deadline.
9. **Slack** — alert to the configured channel.
10. **Final node** — `UPDATE seen_items SET status='done', delivered_at=now()
    WHERE item_uid = ...`. Until this runs the item is still `pending` and the
    worker will re-send it. That is the design (A1), not a bug: a duplicate
    delivery is caught by step 2, a lost lead is not caught by anything.
