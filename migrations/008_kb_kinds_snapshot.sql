-- KB crawler registry gains `kind` (Discourse was the only kind until now) +
-- Snapshot seeded as 6 kb.forums rows, mirroring the `sources` FETCHERS
-- registry pattern (worker/fetchers/__init__.py) but for the archive side.
-- Verified live 2026-08-07 (space ids, proposal counts, ton-society repo state).

-- ── 1. kb.forums.kind ──────────────────────────────────────────────
-- 'site' is reserved, not used yet: woof already lives as a plain row (no
-- crawler needed for our own site) and will get a real crawler later.
-- ADD COLUMN IF NOT EXISTS makes the whole statement a no-op on re-run —
-- the inline CHECK only ever runs the one time the column is actually added.
ALTER TABLE kb.forums
    ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'discourse'
        CHECK (kind IN ('discourse', 'snapshot', 'github', 'site'));

-- ── 2. kb.topics.topic_id: bigint → text ───────────────────────────
-- Snapshot proposal ids are 66-char hex hashes (0x… keccak), not integers —
-- verified live against hub.snapshot.org/graphql 2026-08-07. bigint cannot
-- hold them. Existing Discourse topic ids are small positive integers and
-- stringify losslessly, so UNIQUE (forum_slug, topic_id) keeps working
-- unchanged for every row already archived.
--
-- Guarded via information_schema so a re-run is a no-op: ALTER COLUMN TYPE
-- is not itself IF-NOT-EXISTS-able, and running it twice on an
-- already-text column would just be redundant work, not harmful — but the
-- guard also protects against a partially-upgraded cluster where a manual
-- `\d kb.topics` was used to hand-fix the type out of band.
DO $$
BEGIN
    IF (
        SELECT data_type FROM information_schema.columns
         WHERE table_schema = 'kb' AND table_name = 'topics' AND column_name = 'topic_id'
    ) IS DISTINCT FROM 'text' THEN
        ALTER TABLE kb.topics ALTER COLUMN topic_id TYPE text USING topic_id::text;
    END IF;
END $$;

-- ── 3. Seed 6 curated Snapshot spaces as kb.forums rows ────────────
-- One space per forum row; base_url is the single shared GraphQL host (not
-- per-space) — worker/kb_snapshot.py reads the actual space id out of
-- backfill_cursor->>'space'. Proposal counts are notes-only context, not
-- enforced anywhere.
--
-- lido-snapshot.eth ships disabled: Микола раніше виключив форум Lido з КБ
-- (005_kb_briefs.sql: forum_slug='lido', enabled=false), і той самий рядок
-- лишається вимкненим тут — тому Snapshot-простір Lido сідає enabled=FALSE
-- тим самим рішенням, а не окремим.
INSERT INTO kb.forums
    (forum_slug, base_url, kind, ecosystem, enabled, backfill_done, backfill_cursor, notes)
VALUES
    ('snap-arbitrum', 'https://hub.snapshot.org', 'snapshot', 'Arbitrum', true, false,
     '{"space": "arbitrumfoundation.eth"}'::jsonb,
     'Snapshot-простір arbitrumfoundation.eth — 415 пропозицій (перевірено наживо 2026-08-07)'),

    ('snap-lido', 'https://hub.snapshot.org', 'snapshot', 'Lido', false, false,
     '{"space": "lido-snapshot.eth"}'::jsonb,
     'Snapshot-простір lido-snapshot.eth — 420 пропозицій; вимкнено разом із форумом Lido рішенням Миколи'),

    ('snap-optimism', 'https://hub.snapshot.org', 'snapshot', 'Optimism', true, false,
     '{"space": "opcollective.eth"}'::jsonb,
     'Snapshot-простір opcollective.eth — 93 пропозиції (перевірено наживо 2026-08-07)'),

    ('snap-uniswap', 'https://hub.snapshot.org', 'snapshot', 'Uniswap', true, false,
     '{"space": "uniswapgovernance.eth"}'::jsonb,
     'Snapshot-простір uniswapgovernance.eth — 197 пропозицій (перевірено наживо 2026-08-07)'),

    ('snap-ens', 'https://hub.snapshot.org', 'snapshot', 'ENS', true, false,
     '{"space": "ens.eth"}'::jsonb,
     'Snapshot-простір ens.eth — 98 пропозицій (перевірено наживо 2026-08-07)'),

    ('snap-safe', 'https://hub.snapshot.org', 'snapshot', 'Safe', true, false,
     '{"space": "safe.eth"}'::jsonb,
     'Snapshot-простір safe.eth — 55 пропозицій (перевірено наживо 2026-08-07)')
ON CONFLICT (forum_slug) DO NOTHING;

-- ── 4. Fix the poisoned pipeline seed: ton-society github_discussions ──
-- Verified live 2026-08-07: github.com/ton-society/grants-and-bounties is
-- ARCHIVED and has Discussions disabled — an enabled run would hit the
-- GraphQL API, get an empty/errored `discussions` connection, and silently
-- deliver zero forever, looking exactly like "nothing new" rather than
-- "misconfigured". `sources` has no notes column (only kb.forums does), so
-- the reason lives as a self-documenting key inside `config` — the same
-- jsonb column the admin UI already renders for every source row — instead
-- of a bare SQL comment nobody sees again after this file is applied once.
-- Plain UPDATE with a literal target is idempotent by construction: re-run
-- sets the exact same enabled/config values, nothing accumulates.
UPDATE public.sources
   SET enabled = false,
       config = config || jsonb_build_object(
           'disabled_reason',
           'repo archived, GitHub Discussions disabled — verified live 2026-08-07; do not re-enable without re-checking'
       )
 WHERE type = 'github_discussions'
   AND url = 'https://github.com/ton-society/grants-and-bounties';
