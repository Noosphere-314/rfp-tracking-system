-- Чат-агент 2.0 (KB-Module-Design, чат-розширення продовження 006_chat.sql):
-- сервер-сайд web_search як інструмент чату та /keywords-advice — обидва
-- читають/пишуть лише public.settings, нова таблиця тут не потрібна.
--
--   chat_web_search  'off' за замовчуванням — web_search коштує окремо
--                     ($10/1000 пошуків понад звичайні токени), тож команда
--                     вмикає його свідомо з адмінки, а не отримує по замовчуванню
--                     разом із увімкненням ANTHROPIC_API_KEY.

INSERT INTO public.settings (key, value) VALUES
    ('chat_web_search', 'off')
ON CONFLICT (key) DO NOTHING;
