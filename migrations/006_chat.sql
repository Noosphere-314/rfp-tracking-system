-- Чат-агент над архівом (KB-Module-Design, чат-розширення). Команда ставить
-- запитання з веб-дашборда або з Telegram-бота; MCP-сервіс (mcp/chat.py)
-- відповідає, спираючись на search_kb/get_topic, і кожен обмін репліками
-- лягає сюди — це і історія розмови (для наступного запиту в тому самому
-- сеансі), і матеріал для аудиту вартості (tokens_in/out, денний бюджет).
--
-- session_key зберігає вже НАМЕСПЕЙСЕНИЙ ключ "channel:session_key" (див.
-- HTTP-контракт /chat) — так одна колонка одночасно й ізолює веб-сесії від
-- телеграм-сесій, і дає chat.py робити WHERE session_key = %s без окремого
-- AND channel = %s у кожному запиті; звідси й запас у CHECK (<=200) над
-- API-лімітом session_key (160) — на префікс "telegram:".
--
-- tool_use/tool_result-блоки агентного циклу (виклики search_kb/get_topic
-- усередині однієї відповіді) СВІДОМО НЕ зберігаються — тут лежать лише
-- фінальні репліки user/assistant. Причини: (1) вікно історії, яке читає
-- chat.py, розраховане на строге чергування user/assistant — тулити туди
-- tool-блоки зламало б repair-логіку відновлення вікна; (2) сирі результати
-- search_kb/get_topic важать набагато більше за текст репліки і встигають
-- застаріти до наступного питання того ж сеансу; (3) Claude й так наново
-- звертається до інструментів кожного разу — реплеїти старий tool_result
-- йому не потрібно, а от зберігати зайві мегабайти форумного тексту в кожному
-- рядку — дорого.

CREATE TABLE IF NOT EXISTS kb.chat_messages (
  id bigserial PRIMARY KEY,
  channel text NOT NULL CHECK (channel IN ('web','telegram')),
  session_key text NOT NULL CHECK (char_length(session_key) <= 200),
  who text NOT NULL DEFAULT '' CHECK (char_length(who) <= 64),
  role text NOT NULL CHECK (role IN ('user','assistant')),
  content text NOT NULL CHECK (char_length(content) <= 65536),
  tier text CHECK (tier IN ('llm','stub')),
  model text,
  tokens_in int,
  tokens_out int,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Обидва гарячі шляхи chat.py — вікно історії останніх реплік і лічильник
-- rate-limit за хвилину — фільтрують за session_key і йдуть по id, тож
-- (session_key, id) покриває обидва без окремого індексу під created_at.
CREATE INDEX IF NOT EXISTS kb_chat_messages_session_idx
    ON kb.chat_messages (session_key, id);

-- Runtime-tunable, без редеплою (той самий контракт, що й інші settings).
-- public. — свідомо: n8n має ВЛАСНУ таблицю settings (той самий трюк
-- search_path, що зламав пороги в 626c636), тож тут кожна таблиця з public
-- і kb схем пишеться повним іменем, а не покладається на search_path.
--   chat_model              яку модель Claude використовує LLM-рівень чату
--   chat_daily_token_budget денна стеля tokens_in+tokens_out для ВСЬОГО чату
--                           (глобально, не по сесіях) — понад неї відповіді
--                           деградують до keyword-рівня до півночі.
INSERT INTO public.settings (key, value) VALUES
    ('chat_model',              'claude-sonnet-5'),
    ('chat_daily_token_budget', '300000')
ON CONFLICT (key) DO NOTHING;
