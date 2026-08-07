-- Вердикти класифікатора та win/lost-фідбек (Findings-сторінка, задача розширення).
--
-- ЧАСТИНА A: seen_items переживає items_log. Досі category/confidence/
-- canonical_project писались ЛИШЕ в items_log (подія 'classified',
-- rfp-main.json, нода `Parse verdict` → `Log verdict`) — журнал, який воркер
-- обрізає по ITEMS_LOG_RETENTION_DAYS (90 днів, архітектурний борг, зафіксований
-- у дебаті). Через 90 днів вердикт зникав РАЗОМ із рядком журналу, і на
-- /items не було на чому будувати фільтр за категорією чи впевненістю для
-- старих знахідок. Три нові колонки на seen_items — той самий вердикт, але
-- в таблиці без прунінгу: пишеться n8n-нодами `FINAL: mark done (A1)` і
-- `Mark handled (no lead)` в один момент із фіналізацією статусу (див.
-- rfp-main.json), а тут лише бекфіл для рядків, які фіналізувались ДО цієї
-- міграції.
--
-- outcome/outcome_by/outcome_at — зворотний зв'язок «лід виграно/програно».
-- Без нього нема на чому калібрувати пороги (confidence_threshold,
-- review_band_low): команда бачить, скільки лідів довелось до угоди, лише
-- якщо хтось ЯВНО про це повідомив. Пишеться з /items кнопками Won/Lost
-- (admin/app.py, POST /items/{uid}/outcome) — воркер і n8n цю пару колонок
-- не чіпають.

ALTER TABLE public.seen_items ADD COLUMN IF NOT EXISTS category           text;
ALTER TABLE public.seen_items ADD COLUMN IF NOT EXISTS confidence         real;
ALTER TABLE public.seen_items ADD COLUMN IF NOT EXISTS canonical_project  text;

ALTER TABLE public.seen_items ADD COLUMN IF NOT EXISTS outcome    text
    CHECK (outcome IN ('won', 'lost'));
ALTER TABLE public.seen_items ADD COLUMN IF NOT EXISTS outcome_by text;
ALTER TABLE public.seen_items ADD COLUMN IF NOT EXISTS outcome_at timestamptz;

-- Бекфіл best-effort з items_log: лише для рядків, де category ще NULL (сам
-- цей фільтр і робить UPDATE безпечним для повторного прогону — другий раз
-- WHERE i.category IS NULL не знаходить жодного кандидата). Останній вердикт
-- на item_uid береться DISTINCT ON ... ORDER BY created_at DESC — воркер
-- ретраїть класифікацію, тож на один item_uid подій 'classified' може бути
-- кілька. Рядки, чий items_log вже обрізаний прунінгом (або чий вердикт —
-- STUB без ключа 'category' у payload), просто лишаються NULL — це чесний
-- стан «дані втрачені до того, як з'явилось де їх тримати», а не помилка.
UPDATE public.seen_items i
   SET category          = v.payload ->> 'category',
       confidence         = NULLIF(v.payload ->> 'confidence', '')::real,
       canonical_project  = v.payload ->> 'canonical_project'
  FROM (
        SELECT DISTINCT ON (item_uid) item_uid, payload
          FROM public.items_log
         WHERE event = 'classified'
         ORDER BY item_uid, created_at DESC
       ) v
 WHERE v.item_uid = i.item_uid
   AND i.category IS NULL
   AND v.payload ? 'category';

-- ЧАСТИНА B: архівування бріфів (Briefs-сторінка). NULL = активний, лишається
-- у дефолтному списку /briefs; проставляється POST /briefs/{id}/archive.
ALTER TABLE kb.briefs ADD COLUMN IF NOT EXISTS archived_at timestamptz;
