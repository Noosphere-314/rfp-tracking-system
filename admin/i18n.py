"""Двомовний UI дашборда: англійська за замовчуванням, українська на вибір.

Механізм навмисно мінімальний — жодного gettext, жодних .po-файлів і жодного
кроку збірки. Шаблон кличе `{{ t("nav.overview") }}`, функція шукає ключ у
словнику мови, і при відсутності повертає англійський варіант, а при відсутності
й там — сам ключ. Тобто новий рядок, який забули перекласти, виглядає як
англійський текст (або як помітний ключ), але сторінка не падає.

Вибір мови зберігається в окремій cookie `rfp_lang`, а не в сесії: мова — це
преференція перегляду, а не частина автентифікації. Перезаписувати підписаний
сесійний cookie на кожне перемикання означало б перевидавати сесію заради
косметики, а на екрані логіну (де сесії ще немає) мова все одно потрібна.

Логін — ЗАВЖДИ англійською: це єдиний екран, який може побачити сторонній, і
перемикач там був би зайвою деталлю інтерфейсу до автентифікації.
"""

from __future__ import annotations

from fastapi import Request

DEFAULT_LANG = "en"
LANGS = {"en": "EN", "uk": "UA"}
LANG_COOKIE = "rfp_lang"

# Ключі згруповані за префіксом і читаються як шлях: nav.*, act.* (дії/кнопки),
# f.* (поля й фільтри), st.* (статуси й стани), pg.<сторінка>.* (тексти
# конкретної сторінки), msg.* (повідомлення й порожні стани).
UK: dict[str, str] = {
    # ── навігація й каркас ──────────────────────────────────────────
    "nav.group.work": "Робота",
    "nav.group.cfg": "Налаштування",
    "nav.group.sys": "Система",
    "nav.group.ext": "Зовнішнє",
    "nav.overview": "Огляд",
    "nav.items": "Знахідки",
    "nav.kb": "База знань",
    "nav.chat": "AI-чат",
    "nav.sources": "Джерела",
    "nav.keywords": "Ключові слова",
    "nav.settings": "Параметри",
    "nav.runs": "Історія збору",
    "nav.briefs": "Бріфи",
    "nav.soon": "скоро",
    "nav.aria": "Основна навігація",
    "nav.breadcrumbs": "Хлібні крихти",
    "nav.n8n.aria": "n8n, відкриється у новій вкладці",
    "app.skip": "До вмісту",
    "app.menu": "Меню",
    "app.controls": "Керування",
    "app.logout": "Вийти",
    "app.session.left": "До завершення сесії",
    "app.session.stay": "Залишитись",
    "app.lang": "Мова",
    # ── спільні дії ─────────────────────────────────────────────────
    "act.refresh": "Оновити",
    "act.filter": "Фільтр",
    "act.reset": "Скинути",
    "act.search": "Шукати",
    "act.save": "Зберегти",
    "act.save_all": "Зберегти все",
    "act.add": "Додати",
    "act.enable": "увімкнути",
    "act.disable": "вимкнути",
    "act.copy": "Копіювати",
    "act.copied": "Скопійовано",
    "act.open": "Відкрити",
    "act.all_sources": "усі джерела →",
    "act.all_runs": "уся історія →",
    "act.brief": "Бріф",
    "act.turn_on": "Увімкнути",
    "act.turn_off": "Вимкнути",
    "act.test_save": "Перевірити й зберегти",
    "act.go": "Перейти",
    "act.send": "Надіслати",
    "act.brief.title": "Зібрати бріф із бази знань: пошук у форумному архіві, до 5 хвилин",
    "act.all_queue": "уся черга →",
    "act.newer": "новіші",
    "act.older": "старіші",
    # ── статуси айтемів (значення в URL лишаються англійськими) ─────
    "st.pending": "у черзі",
    "st.done": "оброблено",
    "st.dead": "не доставлено",
    "st.seeded": "засіяно",
    "st.filtered": "відсіяно",
    "st.any": "усі",
    "st.on": "увімкнено",
    "st.off": "вимкнено",
    "st.quarantined": "карантин",
    "st.delivered": "доставлено",
    "st.handled": "оброблено",
    "st.handled.title": "оброблено без створення ліда",
    "st.valid": "валідний",
    "st.invalid": "не компілюється",
    "st.quarantined.label": "Карантин",
    "st.archived": "заархівовано",
    "st.backfill_queued": "бекфіл у черзі",
    "st.empty": "порожній",
    "st.failures": "збоїв",
    "st.running": "триває",
    # ── поля й колонки ──────────────────────────────────────────────
    "f.name": "Назва",
    "f.type": "Тип",
    "f.lane": "Напрям",
    "f.url": "URL",
    "f.state": "Стан",
    "f.status": "Статус",
    "f.title": "Заголовок",
    "f.source": "Джерело",
    "f.ecosystem": "Екосистема",
    "f.category": "Категорія",
    "f.config": "Конфіг",
    "f.attempts": "Спроб",
    "f.first_seen": "Знайдено",
    "f.delivered": "Доставлено",
    "f.pattern": "Патерн",
    "f.kind": "Вид",
    "f.key": "Ключ",
    "f.value": "Значення",
    "f.updated": "Оновлено",
    "f.updated_by": "Ким",
    "f.started": "Початок",
    "f.duration": "Тривалість",
    "f.mode": "Режим",
    "f.items": "Айтемів",
    "f.errors": "Помилок",
    "f.created": "Створено",
    "f.model": "Модель",
    "f.tier": "Рівень",
    "f.forum": "Форум",
    "f.topics": "Тем",
    "f.posts": "Постів",
    "f.limit": "Ліміт",
    "f.search_ph": "Пошук…",
    "f.search": "Пошук",
    "f.config_json": "Конфіг (JSON)",
    "f.action": "Дія",
    "f.reason": "Причина",
    "f.items_7d": "Знахідок за 7 днів",
    "f.last_item": "Остання знахідка",
    "f.consec_fails": "Збоїв поспіль",
    "f.sources_n": "Джерел",
    "f.seen": "Побачено",
    "f.new": "Нових",
    "f.filtered": "Відсіяно",
    "f.dead": "Не доставлено",
    "f.actions": "Дії",
    "f.status_filter": "Фільтр за статусом",
    "f.all_sources": "усі джерела",
    "f.search_title": "Пошук у заголовку",
    "f.search_title_ph": "частина заголовка",
    "f.topic": "Тема",
    "f.snippet": "Фрагмент",
    "f.author": "Автор",
    "f.date": "Дата",
    "f.address": "Адреса",
    "f.freshness": "Свіжість",
    "f.last_crawl": "Останній збір",
    "f.days_short": "дн",
    "f.when": "Коли",
    "f.tool": "Інструмент",
    "f.query": "Запит",
    "f.hits": "Влучань",
    "f.tokens": "токенів",
    "f.item": "Знахідка",
    "f.message": "Повідомлення",
    "f.forum.any": "усі форуми",
    "f.num": "№",
    "f.setting": "Параметр",
    "f.show": "Показувати",
    # Одиниці тривалості — окремими ключами, бо в рядок вони підставляються
    # після числа і в іншій мові можуть мати іншу форму.
    "f.unit.sec": "с",
    "f.unit.min": "хв",
    "f.unit.hour": "год",
    # ── сторінки ────────────────────────────────────────────────────
    "pg.overview.h1": "Огляд",
    "pg.overview.sources": "Стан джерел",
    "pg.overview.runs": "Останні запуски",
    "pg.overview.queue": "У черзі та проблемні",
    "pg.overview.noauto": "Сторінка не оновлюється сама — тисни «Оновити»",
    "pg.overview.sub": "Черга, стан джерел і останні запуски збору",
    "pg.overview.stub": "STUB класифікатор",
    "pg.overview.stub.title": "Anthropic-ноди вимкнені: вердикт захардкоджено, confidence 0.55",
    "pg.overview.worst_shown": "Показано найпроблемніші:",
    "pg.overview.worst_sort": "Сортування — за кількістю збоїв поспіль, далі за давністю останньої знахідки.",
    "pg.overview.add_first_a": "Додай перше на сторінці",
    "pg.overview.add_first_b": "— форма перевіряє його живим тест-фетчем перед збереженням.",
    "pg.items.h1": "Знахідки",
    "pg.items.sub": "Усе, що зібрав воркер: доставлене, відсіяне і те, що чекає",
    "pg.sources.h1": "Джерела",
    "pg.sources.add": "＋ Додати джерело",
    "pg.sources.testing": "Перевіряю джерело…",
    "pg.sources.test_hint": "Джерело зберігається лише якщо тест-фетч повернув хоч один елемент",
    "pg.sources.search_ph": "назва, екосистема, тип, URL",
    "pg.sources.category_hint": "Необов'язково — мітка розділу всередині джерела",
    "pg.sources.config_join": "і мапінг",
    "pg.sources.save_hint": "Джерело, що не пройшло тест-фетч, не зберігається — та сама гарантія, що дає n8n-форма",
    "pg.sources.all": "Усі джерела",
    "pg.sources.empty_hint": "Додай перше через «＋ Додати джерело» вгорі — перед збереженням воно пройде живий тест-фетч.",
    "pg.sources.foot": "Увімкнення знімає карантин: воркер спробує джерело знову на найближчому прогоні.",
    "pg.keywords.h1": "Ключові слова",
    "pg.keywords.sub": "Регекси, що вирішують, які знахідки взагалі доходять до класифікатора",
    "pg.keywords.include": "Пропускають далі",
    "pg.keywords.exclude": "Відсікають",
    "pg.keywords.broken": "Не компілюються",
    "pg.keywords.compile_hint": "Патерн компілюється під час збереження — той, що не компілюється, не приймається",
    "pg.keywords.invalid_h": "Невалідні патерни",
    "pg.keywords.invalid_note": "не застосовуються на прогонах",
    "pg.keywords.invalid_foot": "Редагувати патерн тут не можна: вимкни невалідний і додай виправлений через форму вгорі.",
    "pg.keywords.include_foot": "Знахідка йде до класифікатора, лише якщо збігся хоча б один із цих патернів.",
    "pg.keywords.exclude_foot": "Збіг тут відкидає знахідку ще до класифікатора — exclude виграє в include.",
    "pg.keywords.include_empty": "Патернів пропуску немає",
    "pg.keywords.include_empty_hint": "Поки список порожній, до класифікатора не проходить нічого — додай хоча б один патерн вище.",
    "pg.keywords.exclude_empty": "Патернів відсіву немає",
    "pg.keywords.exclude_empty_hint": "Нічого не відкидається наперед — усе, що пройшло include, іде до класифікатора.",
    "pg.settings.h1": "Параметри",
    "pg.settings.sessions": "Сесії",
    "pg.settings.logout_all": "Завершити всі сесії",
    "pg.settings.logout_all_hint": "Розлогінює всіх, включно з тобою. Природний крок після зміни пароля.",
    "pg.settings.sub": "Значення читають воркер і n8n на кожному прогоні — редеплой не потрібен",
    "pg.settings.search": "Пошук за ключем",
    "pg.settings.search_ph": "напр. threshold",
    # %(low)s / %(high)s підставляє шаблон фільтром replace — порядок чисел у
    # реченні залежить від мови, тож рядок лишається цілим.
    "pg.settings.band.aria": "Нижче %(low)s знахідка ігнорується; від %(low)s до %(high)s іде в канал на перегляд; від %(high)s лід створюється автоматично",
    "pg.settings.band.ignored": "ігнорується",
    "pg.settings.band.review": "у review-канал",
    "pg.settings.band.auto": "лід автоматично",
    "pg.settings.band.hint": "Смуга від 0 до 1 за впевненістю класифікатора. Нижня межа не може бути більшою за поріг автоліда — інакше смуга перегляду порожня і частина знахідок зникає мовчки.",
    "pg.settings.empty": "Параметрів немає",
    "pg.settings.empty_hint": "Таблиця settings порожня — застосуй міграції:",
    "pg.settings.logout_all_when": "Зроби це і тоді, коли є підозра, що чужий пристрій лишився залогіненим. Сам пароль живе в:",
    "pg.runs.h1": "Історія збору",
    "pg.runs.only_errors": "лише з помилками",
    "pg.runs.sub": "Один рядок — один прогін воркера: скільки джерел опитано і що з них вийшло",
    "pg.runs.last50": "50 останніх",
    "pg.runs.last200": "200 останніх",
    "pg.runs.empty_hint": "Історія з'явиться після першого прогону — підніми воркер:",
    # Форми слова «помилка» після числа: 1 → помилка, 2–4 → помилки,
    # 21, 31, … → помилка, решта → помилок. В англійській усі, крім першої,
    # дають "errors", тому гілок чотири, а не три.
    "pg.runs.err_1": "помилка",
    "pg.runs.err_x1": "помилка",
    "pg.runs.err_x24": "помилки",
    "pg.runs.err_many": "помилок",
    "pg.kb.h1": "База знань",
    "pg.kb.results": "Результати",
    "pg.kb.forums": "Форуми в архіві",
    "pg.kb.mcp": "Останні запити MCP",
    "pg.kb.register": "＋ Зареєструвати форум",
    "pg.kb.fresh": "свіжий",
    "pg.kb.stale": "застоявся",
    "pg.kb.sub": "Архів форумів і повнотекстовий пошук по ньому",
    "pg.kb.search_label": "Пошук в архіві",
    "pg.kb.q_ph": 'напр. "developer tooling" grant',
    "pg.kb.register_btn": "Зареєструвати форум",
    "pg.kb.forum_slug": "Ідентифікатор",
    "pg.kb.slug_ph": "напр. lido",
    "pg.kb.slug_hint": "Латиницею, нижнім регістром — під ним форум видно в пошуку і в логах воркера.",
    "pg.kb.base_url": "Адреса форуму",
    "pg.kb.url_hint_a": "Discourse, обов'язково з",
    "pg.kb.url_hint_b": "без слеша в кінці.",
    "pg.kb.register_note_a": "Реєстрація лише ставить форум у чергу. Сам архів піднімає нічна робота",
    "pg.kb.register_note_b": ", далі",
    "pg.kb.register_note_c": "тримає його свіжим.",
    "pg.kb.empty_hint": "Спробуй інші формулювання: синонім («dev tooling» → «developer infrastructure»), англійською замість української, коротшу фразу з одного-двох слів. Пошук індексує англійський текст постів.",
    "pg.kb.empty_link": "AI-чат робитиме ці переформулювання сам",
    "pg.kb.fresh_note": "Свіжість — за найновішою активністю в архіві",
    "pg.kb.no_news": "без новин",
    "pg.kb.confirm_off": "Вимкнути форум",
    "pg.kb.confirm_off2": "Архів перестане оновлюватись.",
    "pg.kb.empty_forums": "Жодного форуму не зареєстровано",
    "pg.kb.empty_forums_hint": "Додай форум формою «Зареєструвати форум» угорі, потім запусти бекфіл:",
    "pg.kb.empty_mcp": "Запитів через MCP ще не було",
    "pg.kb.empty_mcp_hint": "Підключи kbmcp до Claude (див. User-Guide) і постав перше питання — сюди потрапляють запити з чату, а не з форми пошуку вище.",
    "pg.briefs.h1": "Бріфи",
    "pg.briefs.sub": "Довідки по екосистемах, зібрані з архіву форумів",
    "pg.briefs.empty_filter": "За цим фільтром бріфів немає",
    "pg.briefs.empty_filter_hint": "Спробуй іншу екосистему або рівень — або скинь фільтр.",
    "pg.briefs.show_all": "Показати всі бріфи",
    "pg.briefs.empty": "Бріфів ще немає",
    "pg.briefs.empty_hint": "Бріф створюється кнопкою «бріф» на сторінці знахідок або нодою n8n після створення ліда. Потрібен заархівований форум тієї ж екосистеми.",
    "pg.brief.h1": "Бріф",
    "pg.chat.h1": "AI-чат",
    "pg.chat.sub": "Питання до бази знань живою мовою",
    "pg.chat.ph": "Напр.: які гранти на dev tooling обговорювали в Optimism цього кварталу?",
    "pg.chat.empty_title": "Запитай базу знань",
    "pg.chat.empty_hint": "Питання звичайною мовою до архіву форумів: модель сама перебере кілька формулювань, прочитає теми цілком і використає знайдене у відповіді. Той самий архів доступний і повнотекстовим пошуком.",
    "pg.chat.kb_link": "Пошук у Базі знань",
    "pg.chat.thinking": "Друкує…",
    "pg.chat.sending": "Надсилаю…",
    "pg.chat.error": "Не вдалося надіслати повідомлення — спробуй ще раз",
    "pg.chat.new_chat": "Новий чат",
    "pg.chat.stub_chip": "рівень «ключові слова»",
    # ── параметри (SETTING_META в app.py: EN там, UA тут) ───────────
    "setg.classify": "Класифікація",
    "setg.volume": "Обсяг і контроль",
    "setg.channels": "Канали",
    "setg.leads": "Ліди",
    "setg.other": "Інше",
    "set.confidence_threshold.label": "Поріг автоліда",
    "set.confidence_threshold.hint": "0…1. Вище порога лід створюється автоматично. Читає n8n (rfp-main).",
    "set.review_band_low.label": "Нижня межа review-смуги",
    "set.review_band_low.hint": "0…1, не більше за поріг автоліда. Між ними — у канал на перегляд.",
    "set.classifier_prompt_version.label": "Версія промпта",
    "set.classifier_prompt_version.hint": "Поки не використовується: у STUB-режимі класифікатор пише prompt_version сам.",
    "set.max_leads_per_run.label": "Максимум лідів за прогін",
    "set.max_leads_per_run.hint": "1…500. Поки не використовується — жоден компонент цього ключа не читає.",
    "set.lead_floor_7d.label": "Мінімум лідів за 7 днів",
    "set.lead_floor_7d.hint": "0…100. Менше — спрацьовує алерт (читає n8n rfp-digest).",
    "set.source_dark_days.label": "Днів мовчання джерела до алерту",
    "set.source_dark_days.hint": "1…365. Читає воркер на кожному прогоні — нечислове значення зупиняє прогін посеред циклу.",
    "set.alert_channel.label": "Канал алертів",
    "set.alert_channel.hint": "Починається з #. Поки не використовується: Slack-ноди вимкнені, сповіщення йдуть у Telegram.",
    "set.review_channel.label": "Канал перегляду",
    "set.review_channel.hint": "Починається з #. Поки не використовується (Slack-ноди вимкнені).",
    "set.digest_channel.label": "Канал дайджесту",
    "set.digest_channel.hint": "Починається з #. Поки не використовується (Slack-ноди вимкнені).",
    "set.lead_title_template.label": "Шаблон заголовка ліда",
    "set.lead_title_template.hint": "Плейсхолдери {label}, {ecosystem}, {title}. Поки не використовується.",
    "set.chat_model.label": "Модель чату",
    "set.chat_model.hint": "Ідентифікатор моделі Anthropic для AI-чату над базою знань; застосовується без редеплою.",
    # ── повідомлення й порожні стани ────────────────────────────────
    "msg.empty": "Тут поки порожньо",
    "msg.saved": "Збережено",
    "msg.nothing_found": "Нічого не знайдено",
    "msg.no_runs": "Запусків ще не було — підніми воркер:",
    "msg.filter_client": "Фільтри клієнтські: діє той, який змінили останнім",
    "msg.fails_in_row": "невдач поспіль",
    "msg.confirm_disable_src": "Воркер перестане опитувати це джерело.",
    "msg.confirm_logout_all": "Завершити всі сесії? Доведеться увійти заново — і вам теж.",
    "msg.no_reason": "причину не записано",
    "msg.shown": "показано",
    "msg.of": "з",
    "msg.and": "і",
    "msg.no_sources": "Джерел ще немає",
    "msg.no_runs.title": "Запусків ще не було",
    "msg.no_runs.hint": "Підніми воркер:",
    "msg.queue_empty": "Черга порожня",
    "msg.queue_empty.hint": "Усе, що воркер знайшов, уже оброблено — нічого не зависло.",
    "msg.drop_filters": "Спробуй зняти частину фільтрів —",
    "msg.show_all_items": "показати всі знахідки",
    "msg.no_items_yet": "Воркер ще нічого не приніс. Перевір",
    "msg.check_runs": "історію збору",
    "msg.check_sources": "стан джерел",
    "msg.brief_busy": "Готую бріф, до 5 хв…",
    "msg.limited": "(обмежено)",
}

EN_FALLBACK_HINT = "«{key}»"


def normalize(lang: str | None) -> str:
    return lang if lang in LANGS else DEFAULT_LANG


def lang_of(request: Request) -> str:
    """Мова цього запиту. Дефолт — англійська; UA лише за явним вибором."""
    return normalize(request.cookies.get(LANG_COOKIE))


def translator(lang: str):
    """→ функція `t(key, **kwargs)` для шаблонів.

    Англійська не має словника: англійські рядки живуть прямо в шаблонах як
    fallback-аргумент `t("nav.items", "Findings")`. Це тримає англійський текст
    у місці, де його видно при читанні розмітки, і не змушує підтримувати два
    словники синхронно.
    """
    table = UK if lang == "uk" else {}

    def t(key: str, default: str = "") -> str:
        if lang == "uk":
            return table.get(key) or default or EN_FALLBACK_HINT.format(key=key)
        return default or EN_FALLBACK_HINT.format(key=key)

    return t


# Значення з БД (status/lane/kind/mode/tier) — теж UI-текст, тож теж двомовні.
# Ключ будується як "<група>.<значення>", а `.get(x, x)` у шаблонах лишається
# останнім рубежем: нове значення в БД покажеться як є, а не зникне (F13).
VALUE_EN = {
    "st": {
        "pending": "queued",
        "done": "handled",
        "dead": "not delivered",
        "seeded": "seeded",
        "filtered": "filtered out",
    },
    "lane": {"rfp": "RFP", "funding": "funding"},
    "kind": {"include": "lets through", "exclude": "filters out"},
    "mode": {"run": "regular", "tier": "tier", "seed": "seeding"},
    "tier": {"llm": "LLM", "basic": "basic"},
}
VALUE_UK = {
    "st": {
        "pending": "у черзі",
        "done": "оброблено",
        "dead": "не доставлено",
        "seeded": "засіяно",
        "filtered": "відсіяно",
    },
    "lane": {"rfp": "RFP", "funding": "фандинг"},
    "kind": {"include": "пропускає", "exclude": "відсікає"},
    "mode": {"run": "звичайний", "seed": "засів"},
    "tier": {"llm": "LLM", "basic": "базовий"},
}


def value_maps(lang: str) -> dict[str, dict[str, str]]:
    """Словники значень для поточної мови під іменами, які вже є в шаблонах."""
    src = VALUE_UK if lang == "uk" else VALUE_EN
    return {
        "STATUS_UA": src["st"],
        "LANE_UA": src["lane"],
        "KIND_UA": src["kind"],
        "MODE_UA": src["mode"],
        "TIER_UA": src["tier"],
    }


def template_context(request: Request) -> dict:
    lang = lang_of(request)
    return {
        "lang": lang,
        "LANGS": LANGS,
        "t": translator(lang),
        **value_maps(lang),
    }
