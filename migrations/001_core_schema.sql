-- Core application schema (System-Design.md §4, Deployment-Plan Stage 2.1).
-- Everything the pipeline needs lives in `public`; n8n owns the `n8n` schema;
-- the knowledge base will get `kb` in Stage 5.

-- ── sources ────────────────────────────────────────────────────────
-- One row per concrete source. Adding a source of an existing type is a form
-- submission, never a deploy. `config` carries type-specific fields
-- (discourse category paths, snapshot space ids, aggregator field maps).
CREATE TABLE sources (
    id                bigserial PRIMARY KEY,
    type              text        NOT NULL,
    name              text        NOT NULL,
    ecosystem         text        NOT NULL,
    url               text        NOT NULL,
    category          text,
    config            jsonb       NOT NULL DEFAULT '{}'::jsonb,
    lane              text        NOT NULL DEFAULT 'rfp',
    enabled           boolean     NOT NULL DEFAULT true,
    -- Validate-on-read (A4): a row the worker cannot make sense of is
    -- quarantined and alerted, never silently skipped.
    quarantined       boolean     NOT NULL DEFAULT false,
    quarantine_reason text,
    added_by          text        NOT NULL DEFAULT 'migration',
    created_at        timestamptz NOT NULL DEFAULT now(),
    last_attempt_at   timestamptz,
    last_success_at   timestamptz,
    last_item_at      timestamptz,
    consecutive_failures int      NOT NULL DEFAULT 0,
    UNIQUE (type, url, category),
    CONSTRAINT sources_lane_check CHECK (lane IN ('rfp', 'funding'))
);

CREATE INDEX sources_enabled_idx ON sources (enabled) WHERE enabled;

-- ── keywords ───────────────────────────────────────────────────────
-- Free regex pre-filter ahead of the LLM. Patterns are compile-checked with a
-- timeout at write time (form) and again at read time (worker); a bad pattern
-- is marked invalid rather than taking the run down.
CREATE TABLE keywords (
    id             bigserial PRIMARY KEY,
    pattern        text        NOT NULL,
    kind           text        NOT NULL,
    enabled        boolean     NOT NULL DEFAULT true,
    valid          boolean     NOT NULL DEFAULT true,
    invalid_reason text,
    added_by       text        NOT NULL DEFAULT 'migration',
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (pattern, kind),
    CONSTRAINT keywords_kind_check CHECK (kind IN ('include', 'exclude'))
);

-- ── settings ───────────────────────────────────────────────────────
-- Team-editable knobs. Read fresh on every run: no restart to change them.
CREATE TABLE settings (
    key        text PRIMARY KEY,
    value      text        NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text        NOT NULL DEFAULT 'migration'
);

-- ── seen_items ─────────────────────────────────────────────────────
-- The delivery state machine (A1). An item is done only once n8n's final node
-- confirms the Pipedrive lead exists; anything else is retried.
--
--   pending  → sent to n8n, not yet confirmed. Re-sent after the retry window.
--   done     → Pipedrive lead confirmed by the workflow's final node.
--   dead     → exceeded MAX_DELIVERY_ATTEMPTS; alerted, never retried again.
--   seeded   → existed before go-live; recorded for dedup, never delivered (A2).
--   filtered → failed the regex pre-filter; recorded so it is not re-evaluated.
CREATE TABLE seen_items (
    item_uid        text PRIMARY KEY,
    source_id       bigint      NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    external_id     text        NOT NULL,
    status          text        NOT NULL DEFAULT 'pending',
    attempts        int         NOT NULL DEFAULT 0,
    title           text,
    url             text,
    content_hash    text,
    -- Cross-source dedup (A6): the same RFP seen on a forum, on Snapshot and
    -- in an aggregator collapses onto one fingerprint.
    fingerprint     text,
    item_ts         timestamptz,
    first_seen      timestamptz NOT NULL DEFAULT now(),
    last_attempt_at timestamptz,
    delivered_at    timestamptz,
    CONSTRAINT seen_items_status_check
        CHECK (status IN ('pending', 'done', 'dead', 'seeded', 'filtered'))
);

-- Drives the retry sweep; kept small because only pending rows are indexed.
CREATE INDEX seen_items_pending_idx ON seen_items (last_attempt_at NULLS FIRST)
    WHERE status = 'pending';
CREATE INDEX seen_items_fingerprint_idx ON seen_items (fingerprint)
    WHERE fingerprint IS NOT NULL;
CREATE INDEX seen_items_source_idx ON seen_items (source_id, first_seen DESC);

-- ── org_registry ───────────────────────────────────────────────────
-- Race-safe organisation identity (A7). Two concurrent items for the same
-- ecosystem must not create two Pipedrive orgs, so the Postgres upsert — not
-- a Pipedrive search — is the source of truth.
CREATE TABLE org_registry (
    normalized_name  text PRIMARY KEY,
    pipedrive_org_id bigint      NOT NULL,
    display_name     text,
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- ── items_log ──────────────────────────────────────────────────────
-- Append-only trail: what was fetched, what the classifier said, which prompt
-- version said it. Capped at ITEMS_LOG_RETENTION_DAYS by the worker.
CREATE TABLE items_log (
    id             bigserial PRIMARY KEY,
    item_uid       text,
    source_id      bigint REFERENCES sources (id) ON DELETE SET NULL,
    event          text        NOT NULL,
    payload        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    prompt_version text,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX items_log_created_idx ON items_log (created_at DESC);
CREATE INDEX items_log_item_idx ON items_log (item_uid);

-- ── http_cache ─────────────────────────────────────────────────────
-- ETag / Last-Modified per URL. Keyed by URL rather than by source because one
-- source fans out to many endpoints (category pages, topic pages).
CREATE TABLE http_cache (
    url           text PRIMARY KEY,
    etag          text,
    last_modified text,
    fetched_at    timestamptz NOT NULL DEFAULT now()
);

-- ── host_rate_limits ───────────────────────────────────────────────
-- Shared token bucket, consulted by both the pipeline fetchers and (Stage 5)
-- the KB crawler, so one forum never sees double our intended rate.
CREATE TABLE host_rate_limits (
    host        text PRIMARY KEY,
    tokens      double precision NOT NULL,
    rate_per_sec double precision NOT NULL,
    burst       double precision NOT NULL,
    last_refill timestamptz NOT NULL DEFAULT now()
);

-- ── worker_runs ────────────────────────────────────────────────────
-- One row per run. Feeds the daily Slack digest and the lead-floor heartbeat
-- without either of them having to re-derive counts from items_log.
CREATE TABLE worker_runs (
    id               bigserial PRIMARY KEY,
    started_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz,
    mode             text        NOT NULL DEFAULT 'run',
    sources_checked  int         NOT NULL DEFAULT 0,
    sources_failed   int         NOT NULL DEFAULT 0,
    items_seen       int         NOT NULL DEFAULT 0,
    items_new        int         NOT NULL DEFAULT 0,
    items_filtered   int         NOT NULL DEFAULT 0,
    items_delivered  int         NOT NULL DEFAULT 0,
    items_dead       int         NOT NULL DEFAULT 0,
    detail           jsonb       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX worker_runs_started_idx ON worker_runs (started_at DESC);
