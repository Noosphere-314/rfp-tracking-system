-- Briefing packs: the KB answers "what do we know about this ecosystem?"
-- automatically for every lead (User-Guide §9.5, pipeline enhancement).

-- Map pipeline ecosystems onto archived forums. Matching is by this column,
-- not by slug guessing: 'Optimism' (sources.ecosystem) → forum 'optimism'.
ALTER TABLE kb.forums ADD COLUMN IF NOT EXISTS ecosystem text;

UPDATE kb.forums SET ecosystem = 'Optimism' WHERE forum_slug = 'optimism';
UPDATE kb.forums SET ecosystem = 'Arbitrum' WHERE forum_slug = 'arbitrum';
UPDATE kb.forums SET ecosystem = 'Lido'     WHERE forum_slug = 'lido';

-- Generated briefs. Kept (not regenerated on read) so sales can open the same
-- brief the note links to, and so cost/quality can be audited per model.
CREATE TABLE kb.briefs (
    id          bigserial PRIMARY KEY,
    item_uid    text,                -- seen_items ref when generated for a lead
    ecosystem   text NOT NULL,
    title       text NOT NULL,       -- the lead/topic the brief is about
    brief_md    text NOT NULL,
    tier        text NOT NULL CHECK (tier IN ('llm', 'basic')),
    model       text,                -- NULL for basic tier
    tokens_in   int,
    tokens_out  int,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX kb_briefs_item_idx ON kb.briefs (item_uid);
CREATE INDEX kb_briefs_created_idx ON kb.briefs (created_at DESC);

-- Runtime-tunable, no redeploy (same contract as every other setting):
--   brief_model     which Claude model the LLM tier uses
--   brief_language  output language for the sales team
INSERT INTO settings (key, value) VALUES
    ('brief_model',    'claude-opus-5'),
    ('brief_language', 'en')
ON CONFLICT (key) DO NOTHING;
