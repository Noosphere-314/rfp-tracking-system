-- GitHub-KB connector: kb.forums.kind = 'github' (KB-Module-Design.md
-- extension, worker/kb_github.py). 'github' is already a legal kind value —
-- migration 008's CHECK constraint (kb.forums.kind IN ('discourse',
-- 'snapshot', 'github', 'site')) already includes it, so this migration only
-- seeds rows; it does not touch the constraint.
--
-- Потрібен GITHUB_TOKEN (PAT без жодних scopes — публічне читання через
-- GraphQL API вистачає прав анонімного/read-only токена). Без токена
-- worker/kb_github.py пропускає ці три рядки з попередженням у логах і НЕ
-- рахує це збоєм джерела (consecutive_failures не зростає) — очікуваний стан
-- до того, як ops додасть токен, а не помилка джерела.
--
-- Verified live (research) 2026-08-07:
--   filecoin-project/community                — активні GH Discussions
--                                                (~150-400 тредів, 12
--                                                категорій, треди до 326
--                                                коментарів)
--   ethereum-optimism/ecosystem-contributions  — issue-shaped ("Foundation
--                                                Mission Request" лейбл,
--                                                ~40 issues)
--   metaplex-foundation/dao                    — Discussions (~36, grant
--                                                submissions)
--
-- backfill_cursor->>'repo' і ->>'mode' — те, що worker/kb_github.py читає на
-- кожному прогоні (repo ніде більше не зберігається), той самий підхід, що
-- migration 008 використала для kb_snapshot.py і ->>'space'.
--
-- ON CONFLICT (forum_slug) DO NOTHING робить увесь INSERT ідемпотентним —
-- повторний прогін цієї міграції нічого не змінює в уже засіяних рядках.
INSERT INTO kb.forums
    (forum_slug, base_url, kind, ecosystem, enabled, backfill_done, backfill_cursor)
VALUES
    ('gh-filecoin', 'https://github.com/filecoin-project/community', 'github', 'Filecoin',
     true, false, '{"repo": "filecoin-project/community", "mode": "discussions"}'::jsonb),

    ('gh-op-missions', 'https://github.com/ethereum-optimism/ecosystem-contributions', 'github', 'Optimism',
     true, false, '{"repo": "ethereum-optimism/ecosystem-contributions", "mode": "issues"}'::jsonb),

    ('gh-metaplex', 'https://github.com/metaplex-foundation/dao', 'github', 'Metaplex',
     true, false, '{"repo": "metaplex-foundation/dao", "mode": "discussions"}'::jsonb)
ON CONFLICT (forum_slug) DO NOTHING;
