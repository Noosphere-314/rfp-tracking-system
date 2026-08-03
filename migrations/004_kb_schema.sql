-- Knowledge Base ("research mode") — KB-Module-Design.md.
-- Separate schema: this data answers "what do we know?", is reproducible by
-- re-crawling, and is excluded from pg_dump (except kb.forums crawl state).

CREATE SCHEMA IF NOT EXISTS kb;

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ── Crawl registry ─────────────────────────────────────────────────
-- Which forums we archive, and where the crawler left off. Distinct from
-- `sources` (the RFP pipeline watches specific *categories* hourly; the KB
-- archives *whole forums* on its own cadence).
CREATE TABLE kb.forums (
    id              serial PRIMARY KEY,
    forum_slug      text NOT NULL UNIQUE,          -- short handle: 'optimism'
    base_url        text NOT NULL,                 -- https://gov.optimism.io
    enabled         boolean NOT NULL DEFAULT true,
    -- NULL = archive every category; else an int[] whitelist (Cardano case:
    -- general community forum where only governance categories are wanted).
    category_ids    int[],
    backfill_done   boolean NOT NULL DEFAULT false,
    -- Backfill resume point: category list position + page, so an interrupted
    -- overnight crawl continues instead of restarting.
    backfill_cursor jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- Incremental watermark: newest post seen via /posts.json.
    last_post_seen_at   timestamptz,
    last_incremental_at timestamptz,
    consecutive_failures int NOT NULL DEFAULT 0,
    notes           text,
    added_at        timestamptz NOT NULL DEFAULT now()
);

-- ── Archive ────────────────────────────────────────────────────────
CREATE TABLE kb.topics (
    id              bigserial PRIMARY KEY,
    forum_slug      text NOT NULL,
    topic_id        bigint NOT NULL,
    category_id     int,
    category_name   text,
    title           text NOT NULL,
    url             text NOT NULL,
    author          text,
    created_at      timestamptz,
    bumped_at       timestamptz,
    post_count      int,
    last_crawled_at timestamptz NOT NULL DEFAULT now(),
    title_tsv       tsvector GENERATED ALWAYS AS (to_tsvector('english', title)) STORED,
    UNIQUE (forum_slug, topic_id)
);

CREATE TABLE kb.posts (
    id           bigserial PRIMARY KEY,
    topic_ref    bigint NOT NULL REFERENCES kb.topics(id) ON DELETE CASCADE,
    post_number  int NOT NULL,
    author       text,
    posted_at    timestamptz,
    edited_at    timestamptz,
    raw_text     text NOT NULL,   -- stripped plain text, feeds FTS
    raw_html     text,            -- cooked HTML kept verbatim for faithful citation
    body_tsv     tsvector GENERATED ALWAYS AS (to_tsvector('english', raw_text)) STORED,
    -- future semantic upgrade lands here via ALTER TABLE ADD COLUMN embedding
    UNIQUE (topic_ref, post_number)
);

CREATE INDEX kb_posts_body_tsv_idx  ON kb.posts  USING GIN (body_tsv);
CREATE INDEX kb_topics_title_tsv_idx ON kb.topics USING GIN (title_tsv);
CREATE INDEX kb_topics_title_trgm_idx ON kb.topics USING GIN (title gin_trgm_ops);
CREATE INDEX kb_topics_bumped_idx    ON kb.topics (forum_slug, bumped_at DESC);
-- No extra (topic_ref, post_number) index: the UNIQUE constraint above already
-- provides exactly that index.

-- ── Q&A instrumentation (KB-Module-Design §7) ─────────────────────
-- Every search the MCP server answers is logged; two weeks of real bid-prep
-- use decides whether the embeddings trigger fires. Without this table the
-- semantic-upgrade decision would be vibes, not data.
CREATE TABLE kb.query_log (
    id          bigserial PRIMARY KEY,
    asked_at    timestamptz NOT NULL DEFAULT now(),
    tool        text NOT NULL,             -- search_kb / get_topic / live_forum_search
    query       text,
    forum_slug  text,
    hits        int,
    note        text                       -- e.g. explicit "missed X" feedback
);

-- ── Seed: MVP archives (KB-Module-Design §10) ─────────────────────
INSERT INTO kb.forums (forum_slug, base_url, enabled, notes) VALUES
    ('optimism', 'https://gov.optimism.io',            true,
     '2.5k topics / 44k posts — MVP archive #1'),
    ('arbitrum', 'https://forum.arbitrum.foundation',  true,
     '2.7k topics / 73k posts — MVP archive #2'),
    ('lido',     'https://research.lido.fi',           false,
     '1.3k topics — enable after MVP validated')
ON CONFLICT (forum_slug) DO NOTHING;

-- Cardano deliberately absent until its governance/Catalyst category ids are
-- researched (general forum, 42k topics — archiving all of it is wrong).
