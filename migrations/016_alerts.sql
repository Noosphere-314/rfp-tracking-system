-- 016: алерти воркера в БД (рішення Миколи 2026-08-31: «хай зберігаються
-- в базі даних — та й взагалі ми все там зберігаємо»).
--
-- До цього worker/alerts.py слав ТІЛЬКИ в Slack-webhook, який ніколи не був
-- налаштований — «Filecoin падає в кожному прогоні» тижнями жило лише в
-- docker logs. Тепер кожен алерт: (1) рядок тут — видимий на /runs;
-- (2) приватне повідомлення в Telegram-бот (НЕ в групу), з дедупом по
-- тексту, щоб щогодинний повтор того самого провалу не став спамом.

CREATE TABLE alerts (
    id         bigserial PRIMARY KEY,
    level      text NOT NULL CHECK (level IN ('info', 'warning', 'error')),
    message    text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Обидва читачі: /runs (останні за часом) і дедуп-перевірка перед TG
-- (те саме message за останні N годин).
CREATE INDEX alerts_created_idx ON alerts (created_at DESC);
CREATE INDEX alerts_message_recent_idx ON alerts (message, created_at DESC);
