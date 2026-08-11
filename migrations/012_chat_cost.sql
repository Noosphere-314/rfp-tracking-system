-- Важелі економії чату (2026-08-11, продовження оптимізації токенів) +
-- статистика покриття KB-архівів.
--
-- 1. kb.chat_cache — кеш відповідей на ПОВТОРЮВАНІ перші питання розмови.
--    Команда часто питає схоже ("які гранти на дев-тулінг в Optimism?") —
--    друга людина з тим самим питанням у межах TTL отримує готову відповідь
--    за нуль токенів. Кешується ЛИШЕ перший хід сесії (без історії): відповідь
--    на follow-up залежить від усієї розмови, і ключа для неї не існує.
--    Ключ: нормалізоване питання + відсортований scope форумів + web-прапорець
--    (відповідь із веб-пошуком і без — різні відповіді, змішувати не можна).
--
-- 2. chat_model_light — дешевша модель для простих навігаційних питань
--    ("знайди тред про X"): роутинг в mcp/chat.py, порожнє значення вимикає.
--    chat_cache_ttl_hours — строк життя кешу; 0 вимикає кеш повністю.
--
-- 3. kb.forums.remote_* — еталонні лічильники з /about.json форуму
--    (topics_count/posts_count), які краулер оновлює на кожному проході:
--    знайдено 2026-08-11, що архіви СУТТЄВО неповні (пости ~44-55%), і без
--    еталона поруч із нашими цифрами на /kb цього не видно взагалі.
--
-- public. — свідомо: n8n має ВЛАСНУ таблицю settings (search_path-трюк,
-- що зламав пороги в 626c636), тож пишемо повним іменем, як 009/011.

CREATE TABLE IF NOT EXISTS kb.chat_cache (
    id            bigserial PRIMARY KEY,
    question_norm text        NOT NULL,
    scope         text        NOT NULL DEFAULT '',   -- 'arbitrum,compound' або ''
    web           boolean     NOT NULL DEFAULT false,
    reply_md      text        NOT NULL,
    model         text,
    tokens_in     integer,
    tokens_out    integer,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Пошук завжди «останній свіжий запис за точним ключем» — DESC по created_at
-- у складеному індексі робить його одним index-scan без сортування.
CREATE INDEX IF NOT EXISTS kb_chat_cache_lookup_idx
    ON kb.chat_cache (question_norm, scope, web, created_at DESC);

ALTER TABLE kb.forums
    ADD COLUMN IF NOT EXISTS remote_topics integer,
    ADD COLUMN IF NOT EXISTS remote_posts  integer,
    ADD COLUMN IF NOT EXISTS stats_at      timestamptz;

INSERT INTO public.settings (key, value) VALUES
    ('chat_model_light',     'claude-haiku-4-5-20251001'),
    ('chat_cache_ttl_hours', '24')
ON CONFLICT (key) DO NOTHING;
