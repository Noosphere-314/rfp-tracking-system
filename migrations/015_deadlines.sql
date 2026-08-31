-- 015: дедлайн-трекер (план покращень 2026-08-31, функціональність п.2).
--
-- Гранти живуть дедлайнами, а ми їх лише згадували текстом у тижневих
-- звітах — «побачили, обговорили, проґавили вікно» був реальним ризиком.
-- Джерело рядків — щотижневий grants-звіт (mcp/weekly.py): модель і так
-- знаходить дедлайни, тепер віддає їх окремим машинним блоком, а ми
-- складаємо в цю таблицю. Споживачі: блок «Closing soon» на Overview і
-- щоденний Telegram-пінг за 3 дні до дедлайну (n8n rfp-digest).
--
-- (title, deadline) — природний ключ: та сама програма з тим самим вікном
-- не має плодити рядки від кожного тижневого прогону; НОВЕ вікно тієї ж
-- програми (наступний раунд) — легітимний окремий рядок.

CREATE TABLE kb.deadlines (
    id           bigserial PRIMARY KEY,
    title        text NOT NULL,
    ecosystem    text NOT NULL DEFAULT '',
    deadline     date NOT NULL,
    url          text NOT NULL DEFAULT '',
    -- Звідки рядок узявся: 'weekly' (звіт) — на майбутнє і ручні.
    source       text NOT NULL DEFAULT 'weekly',
    -- Ручне «прибрати з очей» (подали заявку / нерелевантно) — рядок
    -- лишається для історії, з Overview і пінгів зникає.
    dismissed_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX kb_deadlines_title_date_idx ON kb.deadlines (title, deadline);
CREATE INDEX kb_deadlines_open_idx ON kb.deadlines (deadline) WHERE dismissed_at IS NULL;
