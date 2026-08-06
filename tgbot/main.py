"""Другий Telegram-бот — чат-фронт до KB-агента (kbmcp).

НЕ плутати з ботом n8n (worker/alerts.py, воркфлоу тривог): той шле алерти в
групу "Woof RFP Feed" з окремим токеном. Цей бот — приватний чат: людина з
дозволеного списку пише питання про Web3-форуми, бот пересилає його в
`{KBMCP_URL}/chat` і повертає відповідь (з посиланнями на джерела).

Архітектура навмисно проста — long polling, не webhook:
  - жодного публічного порту й TLS для самого бота (кожен вебхук вимагає https);
  - "restart: unless-stopped" сам піднімає процес після падіння чи рестарту хоста.
Ціна: рівно ОДИН полер на токен. Другий getUpdates із того ж токена одразу
ловить 409 від Telegram — тому dev і prod не можуть слухати той самий токен
одночасно (division of concerns: BotFather-токен — окремий на кожне оточення).

Потік для одного повідомлення:
  getUpdates → allowlist-фільтр → POST {KBMCP_URL}/chat → post-processing
  (markdown-посилання → звичайний текст, розбиття на шматки під ліміт
  Telegram) → sendMessage по одному на шматок.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import time
from dataclasses import dataclass, field
from types import FrameType

import httpx

log = logging.getLogger("tgbot")

TELEGRAM_API = "https://api.telegram.org"

START_TEXT = (
    "Hi! I'm the RFP knowledge-base assistant.\n\n"
    "Ask me anything about the Web3 governance-forum archive in plain "
    "language and I'll look it up for you — answers cite their sources.\n\n"
    "Send /new any time to start a fresh conversation."
)
NEW_SESSION_TEXT = "Started a new conversation. Ask away."
EMPTY_REPLY_TEXT = "(no answer came back — please try rephrasing)"


# ── Конфігурація (стиль worker/config.py: читання env, нічого не падає на
# імпорті — падати на порожньому CHAT_TELEGRAM_TOKEN тут не можна, інакше
# `restart: unless-stopped` перетворить відсутній токен на crash-loop) ─────


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def parse_allowlist(raw: str) -> set[int]:
    """CHAT_TELEGRAM_ALLOWED_IDS → множина числових Telegram user id.

    Толерантно до пробілів і порожніх сегментів ("1, 2,,3 " → {1, 2, 3}), бо
    список зазвичай редагують руками в .env. Нечислові сегменти пропускаються
    з попередженням — краще втратити один id, ніж упасти на старті.
    """
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            log.warning("CHAT_TELEGRAM_ALLOWED_IDS: skipping non-numeric segment %r", part)
    return ids


@dataclass(frozen=True)
class Config:
    telegram_token: str = field(default_factory=lambda: _env("CHAT_TELEGRAM_TOKEN"))
    allowed_ids: set[int] = field(
        default_factory=lambda: parse_allowlist(_env("CHAT_TELEGRAM_ALLOWED_IDS"))
    )
    kbmcp_url: str = field(default_factory=lambda: _env("KBMCP_URL", "http://kbmcp:8000"))
    # Порожній токен: усе одно стартуємо. kbmcp сам поверне 403, і користувач
    # побачить той текст помилки — це чесніше, ніж мовчки не піднімати бота.
    kb_mcp_token: str = field(default_factory=lambda: _env("KB_MCP_TOKEN"))


# ── Пост-обробка відповіді (чисті функції — саме те, що покрито тестами) ───


# Без вкладених дужок у мітці: "[a [b] c](url)" навмисно НЕ матчиться і
# лишається як є — краще залишити сирий markdown, ніж скалічити текст
# неправильним розбором.
_MD_LINK_RE = re.compile(r"\[([^\[\]]+)\]\(([^()]+)\)")


def md_links_to_plain(text: str) -> str:
    """[label](url) → "label: url" — plain text, без parse_mode.

    parse_mode навмисно не використовується: одна незбалансована сутність
    (наприклад "*" без пари в тексті форуму) валить sendMessage цілком і
    відповідь просто не доходить до людини. Голий текст + Telegram-авто-
    лінкіфікація URL — менш красиво, зате ніколи не падає.
    """
    return _MD_LINK_RE.sub(lambda m: f"{m.group(1)}: {m.group(2)}", text)


def _utf16_len(s: str) -> int:
    """Довжина рядка в UTF-16 code units — саме так Telegram рахує ліміти
    (4096 на sendMessage), а не в символах Python. Астральні символи (більшість
    емодзі) — це 1 python-символ, але 2 code units."""
    return len(s.encode("utf-16-le")) // 2


def _find_cut(s: str, limit: int) -> int:
    """Позиція розрізу (в python-символах) для одного шматка `s`, коли `s`
    цілком не влазить у `limit` code units.

    Пріоритет межі: порожній рядок (абзац) → одинарний перенос рядка →
    жорсткий розріз. Перші два варіанти автоматично не рвуть рядок "label:
    url" посередині — рвуть ПЕРЕД ним, штовхаючи цілий рядок у наступний
    шматок (якщо сам рядок довший за ліміт — розріз усередині неминучий).
    """
    # Максимальний префікс у символах, що влазить у ліміт code units.
    hi = len(s)
    total = 0
    for i, ch in enumerate(s):
        total += len(ch.encode("utf-16-le")) // 2
        if total > limit:
            hi = i
            break
    hi = max(hi, 1)  # запобіжник від нульового просування циклу вище

    para = s.rfind("\n\n", 0, hi)
    if para > 0:
        return para

    nl = s.rfind("\n", 0, hi)
    if nl > 0:
        return nl

    return hi


def chunk_message(text: str, limit: int = 3500) -> list[str]:
    """Розбити `text` на шматки, кожен ≤ `limit` UTF-16 code units.

    Порожній рядок — це "нічого слати", тож повертає []. Ліміт 3500, не 4096
    (реальна межа Telegram) — запас під `label: url` рядки джерел, що інакше
    могли б штовхнути шматок за 4096 просто через авто-лінкіфікацію.
    """
    if not text:
        return []

    chunks: list[str] = []
    remaining = text
    while remaining:
        if _utf16_len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = _find_cut(remaining, limit)
        chunks.append(remaining[:cut].rstrip("\n"))
        remaining = remaining[cut:].lstrip("\n")
    return chunks


# ── Ротація сесії (/new) ────────────────────────────────────────────
#
# In-memory dict chat_id -> unix ts останньої ротації. Свідомий v1-компроміс:
# після рестарту контейнера мапа порожня, і session_key відкочується до
# "голого" chat_id (це не помилка — kbmcp просто побачить продовження старої
# розмови; втрата лише в тому, що /new "забувається").


def session_key(chat_id: int, rotations: dict[int, int]) -> str:
    """session_key для kbmcp. Без двокрапок (вимога протоколу) — chat_id може
    бути від'ємним для груп ("-100...") і мінус тут не проблема, лише "-100..."
    сам по собі не містить ":". До першої ротації — голий str(chat_id)."""
    suffix = rotations.get(chat_id)
    if suffix is None:
        return str(chat_id)
    return f"{chat_id}-{suffix}"


def rotate_session(chat_id: int, rotations: dict[int, int], now: int | None = None) -> str:
    """/new — нова сесія. `now` параметризований заради тестів (без monkeypatch
    time.time); мутує `rotations` на місці — це і є стан процесу."""
    rotations[chat_id] = now if now is not None else int(time.time())
    return session_key(chat_id, rotations)


# ── Telegram Bot API (тонкі обгортки над httpx) ─────────────────────


def _api_url(token: str, method: str) -> str:
    return f"{TELEGRAM_API}/bot{token}/{method}"


def get_updates(
    client: httpx.Client,
    token: str,
    offset: int,
    timeout: int,
    allowed_updates: list[str] | None = None,
) -> list[dict]:
    """POST, не GET: масив allowed_updates інакше довелось би вручну
    JSON-кодувати в query string. Read timeout на самому HTTP-запиті має бути
    ЗА `timeout` секунд довшим за сам long-poll — інакше httpx рве з'єднання
    рівно тоді, коли Telegram ще тримає його відкритим в очікуванні апдейту."""
    payload: dict = {"offset": offset, "timeout": timeout}
    if allowed_updates is not None:
        payload["allowed_updates"] = allowed_updates
    response = client.post(_api_url(token, "getUpdates"), json=payload, timeout=timeout + 10)
    response.raise_for_status()
    return response.json()["result"]


def send_message(
    client: httpx.Client,
    token: str,
    chat_id: int,
    text: str,
    disable_preview: bool = True,
) -> None:
    """Best-effort: помилка sendMessage логується, а не піднімається — інакше
    один зіпсований чанк відповіді валив би обробку решти update-ів."""
    try:
        response = client.post(
            _api_url(token, "sendMessage"),
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": disable_preview,
            },
            timeout=20,
        )
        if response.status_code != 200:
            log.warning("sendMessage failed (%s): %s", response.status_code, response.text[:300])
    except httpx.HTTPError as exc:
        log.warning("sendMessage network error: %s", exc)


def send_typing(client: httpx.Client, token: str, chat_id: int) -> None:
    try:
        client.post(
            _api_url(token, "sendChatAction"),
            json={"chat_id": chat_id, "action": "typing"},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        log.debug("sendChatAction failed (non-fatal): %s", exc)


# ── kbmcp ─────────────────────────────────────────────────────────


def ask_kbmcp(
    client: httpx.Client, cfg: Config, key: str, who: str, message: str
) -> tuple[bool, str]:
    """POST {KBMCP_URL}/chat. Повертає (ok, текст-для-користувача):
    ok=True — payload["reply_md"] як є; ok=False — payload["error"] (вже
    англійською від kbmcp) або наш власний текст на випадок мережевого збою."""
    try:
        response = client.post(
            f"{cfg.kbmcp_url}/chat",
            json={
                "channel": "telegram",
                "session_key": key,
                "who": who,
                "message": message[:4000],
            },
            headers={"Authorization": f"Bearer {cfg.kb_mcp_token}"} if cfg.kb_mcp_token else {},
            timeout=300,  # LLM-рівень легітимно займає хвилини (як admin/app.py /brief)
        )
    except httpx.HTTPError as exc:
        log.warning("kbmcp request failed: %s", exc)
        return False, "Sorry, I could not reach the knowledge base right now. Please try again shortly."

    try:
        payload = response.json()
    except ValueError:
        log.warning("kbmcp returned non-JSON (%s): %s", response.status_code, response.text[:300])
        return False, "Sorry, the knowledge base returned an unexpected response."

    if payload.get("ok"):
        return True, payload.get("reply_md") or ""
    return False, payload.get("error") or "Sorry, something went wrong on the knowledge-base side."


# ── Обробка одного update ───────────────────────────────────────────


def handle_update(
    client: httpx.Client, cfg: Config, sessions: dict[int, int], update: dict
) -> None:
    message = update.get("message")
    if not message or "text" not in message:
        return  # фото/стікери/edited_message тощо — v1 обробляє лише текст

    chat = message.get("chat") or {}
    if chat.get("type") != "private":
        return  # групи навмисно вирізані з v1 — семантика звертання в групі
        # (кому адресовано повідомлення?) окрема задача, не тут

    from_user = message.get("from") or {}
    user_id = from_user.get("id")
    if user_id not in cfg.allowed_ids:
        # Мовчки, без відповіді: чужому не варто навіть натякати, що тут щось
        # відповідає. Рівень info — оператор бачить id і додає в allowlist.
        log.info("ignoring message from non-allowlisted telegram user id=%s", user_id)
        return

    chat_id = chat["id"]
    text = message["text"].strip()
    token = cfg.telegram_token

    if text == "/start":
        send_message(client, token, chat_id, START_TEXT)
        return
    if text == "/new":
        rotate_session(chat_id, sessions)
        send_message(client, token, chat_id, NEW_SESSION_TEXT)
        return
    if not text:
        return

    send_typing(client, token, chat_id)
    ok, reply = ask_kbmcp(client, cfg, session_key(chat_id, sessions), str(user_id), text)
    plain = md_links_to_plain(reply)
    chunks = chunk_message(plain) or [EMPTY_REPLY_TEXT]

    last = len(chunks) - 1
    for i, chunk in enumerate(chunks):
        send_message(client, token, chat_id, chunk, disable_preview=(i != last))
    if not ok:
        log.info("kbmcp returned an error for chat_id=%s: %s", chat_id, reply)


# ── Головний цикл ────────────────────────────────────────────────────

_stopping = False


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    global _stopping
    _stopping = True
    log.info("received signal %s; stopping after the current batch", signum)


def _interruptible_sleep(seconds: float) -> None:
    """time.sleep одним шматком означало б чекати на SIGTERM до `seconds`
    секунд — цикл секундними кроками дає graceful shutdown реагувати швидко."""
    slept = 0.0
    step = 1.0
    while slept < seconds and not _stopping:
        time.sleep(min(step, seconds - slept))
        slept += step


def drain_backlog(client: httpx.Client, token: str) -> int | None:
    """Стартовий дренаж черги. Telegram тримає недоставлені update-и ~24
    години; якщо контейнер простояв і полінг просто продовжив би з останнього
    offset, бот прочитав би (і, гірше, оплатив LLM-викликами) цілу добу
    накопичених повідомлень одразу після старту.

    offset=-1 — задокументований трюк getUpdates: повертає щонайбільше ОДИН,
    найновіший update, не чіпаючи його з черги. Точну кількість пропущених
    update-ів ця техніка не дає (Telegram її просто не повертає) — логуємо
    сам факт дренажу, а не число.
    """
    try:
        updates = get_updates(client, token, offset=-1, timeout=0)
    except httpx.HTTPError as exc:
        log.warning("startup backlog check failed (continuing from offset 0): %s", exc)
        return None
    if not updates:
        return None
    last_id = updates[-1]["update_id"]
    log.info(
        "startup: skipping backlog accumulated during downtime, resuming after update_id=%d",
        last_id,
    )
    return last_id + 1


def _sleep_forever() -> None:
    """CHAT_TELEGRAM_TOKEN порожній: немає з чим полити. Падати (raise) тут не
    можна — compose піднімає сервіс з `restart: unless-stopped`, і падіння
    означало б нескінченний crash-loop замість одного чіткого попередження в
    логах."""
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    while not _stopping:
        _interruptible_sleep(3600)


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # httpx на INFO логує повний URL кожного запиту, а токен Telegram-бота
    # живе саме в URL (/bot<token>/getUpdates) — без цього рядка секрет
    # витікав би в docker logs кожні 50 секунд.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = Config()

    if not cfg.telegram_token:
        log.warning(
            "CHAT_TELEGRAM_TOKEN не задано — бот не може почати polling; сплю "
            "замість падіння (див. docstring _sleep_forever)"
        )
        _sleep_forever()
        return

    if not cfg.allowed_ids:
        log.warning(
            "CHAT_TELEGRAM_ALLOWED_IDS порожній — усі вхідні повідомлення "
            "ігноруватимуться мовчки, поки список не заповнять"
        )

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    sessions: dict[int, int] = {}
    with httpx.Client() as client:
        offset = drain_backlog(client, cfg.telegram_token) or 0

        log.info("tgbot polling started")
        while not _stopping:
            try:
                updates = get_updates(
                    client, cfg.telegram_token, offset, timeout=50, allowed_updates=["message"]
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 409:
                    log.warning(
                        "409 Conflict from Telegram getUpdates — ANOTHER POLLER IS "
                        "RUNNING WITH THIS TOKEN. Dev and prod must not poll the same "
                        "bot token simultaneously."
                    )
                    _interruptible_sleep(60)
                else:
                    log.warning("getUpdates HTTP error: %s", exc)
                    _interruptible_sleep(5)
                continue
            except httpx.HTTPError as exc:
                log.warning("getUpdates network error: %s", exc)
                _interruptible_sleep(5)
                continue
            except (ValueError, KeyError) as exc:
                log.warning("getUpdates returned an unexpected payload: %s", exc)
                _interruptible_sleep(5)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                try:
                    handle_update(client, cfg, sessions, update)
                except Exception:  # noqa: BLE001 — один поганий update не має вбивати цикл
                    log.exception("unhandled error processing update_id=%s", update.get("update_id"))

    log.info("tgbot stopped")


if __name__ == "__main__":
    run()
