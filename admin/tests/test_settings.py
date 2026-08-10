"""Тести /settings — help/reco на кожному параметрі (розділ A: запит Миколи
«нічого не ясно, потрібні підказки і рекомендації») і сам SETTING_META.

Без БД: `admin.app.db` підмінений monkeypatch'ем на об'єкт, що повертає
готові рядки settings (той самий прийом, що й у test_items.py/test_chat.py).
Сесія — під загальним `test_every_get_route_requires_a_session` у
test_auth.py, тут не дублюється.
"""

from __future__ import annotations

from datetime import datetime, timezone

from admin.tests.test_auth import WHO, _login, client  # noqa: E402,F401

from admin import app as admin_app  # noqa: E402
from admin.app import SETTING_GROUPS, SETTING_META, validate_setting  # noqa: E402


class _Cursor:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Conn:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        return _Cursor(self.rows)

    def commit(self):
        pass


def _settings_row(key: str, value: str) -> dict:
    return {
        "key": key,
        "value": value,
        "updated_at": datetime.now(timezone.utc),
        "updated_by": f"admin-ui:{WHO}",
    }


# ── SETTING_META: дані для help/reco присутні на кожен ключ ──────────────


def test_setting_meta_has_help_and_reco_for_every_key():
    """Розділ A вимагає help+reco для «кожного параметра», а не вибірково —
    падає, якщо хтось додасть новий ключ у SETTING_META і забуде або те, або
    інше."""
    missing = [
        key for key, meta in SETTING_META.items()
        if not meta.get("help") or not meta.get("reco")
    ]
    assert not missing, f"без help/reco: {missing}"


def test_setting_meta_covers_the_keys_explicitly_named_in_the_task():
    """chat_model / chat_daily_token_budget / brief_model / brief_language —
    саме ті ключі, які раніше падали у загальний "other"-кошик без жодного
    пояснення (розділ A)."""
    for key in ("chat_model", "chat_daily_token_budget", "brief_model", "brief_language"):
        assert key in SETTING_META, key
        assert SETTING_META[key]["group"] != "other", (
            f"{key}: лишився в загальному кошику 'other' без власної групи"
        )


def test_setting_groups_only_reference_known_group_ids():
    group_ids = {gid for gid, _ in SETTING_GROUPS}
    for key, meta in SETTING_META.items():
        assert meta["group"] in group_ids, f"{key}: невідома група {meta['group']!r}"


# ── GET /settings: рендер help + reco для відомого ключа ─────────────────


def test_settings_page_renders_help_and_reco_for_a_known_key(client, monkeypatch):
    _login(client)
    conn = _Conn([_settings_row("confidence_threshold", "0.7")])
    monkeypatch.setattr(admin_app, "db", lambda: conn)

    response = client.get("/settings")
    assert response.status_code == 200
    html = response.text

    meta = SETTING_META["confidence_threshold"]
    # Ярлик і уривок help-тексту (весь текст — довгий, перевіряємо стійкий
    # фрагмент, а не рядок цілком).
    assert meta["label"] in html
    assert "Confidence score above which a finding becomes a lead" in html
    # reco рендериться окремою піґулкою з підписом pg.settings.reco_label.
    assert "Suggested" in html
    assert "0.7" in html and "0.7–0.8 is a reasonable start" in html


def test_settings_page_renders_collapsible_explain_intro(client, monkeypatch):
    """Розгортний «What do these settings do?» — plain <details>, без JS
    (розділ A): сам HTML-елемент і англійський дефолт тексту мають бути на
    сторінці навіть без жодного settings-рядка в БД."""
    _login(client)
    monkeypatch.setattr(admin_app, "db", lambda: _Conn([]))

    html = client.get("/settings").text
    assert '<details class="disclosure">' in html
    assert "What do these settings do?" in html


def test_settings_page_has_no_leftover_hint_key_rendering(client, monkeypatch):
    """Регрес на перейменування SETTING_META['hint'] -> 'help'+'reco': сторінка
    не має падати і не повинна лишати сирий ключ set.<key>.hint видимим."""
    _login(client)
    conn = _Conn([_settings_row("chat_daily_token_budget", "300000")])
    monkeypatch.setattr(admin_app, "db", lambda: conn)

    response = client.get("/settings")
    assert response.status_code == 200
    assert "set.chat_daily_token_budget.hint" not in response.text


# ── chat_web_search (розділ C, з'являється після міграції 009) ───────────


def test_chat_web_search_is_in_the_ai_group_with_help_and_reco():
    meta = SETTING_META["chat_web_search"]
    assert meta["group"] == "ai"
    assert meta["label"] == "Chat web search"
    assert meta["help"] and meta["reco"]


def test_chat_web_search_validates_on_off_only():
    assert validate_setting("chat_web_search", "on") == (True, "")
    assert validate_setting("chat_web_search", "off") == (True, "")
    ok, err = validate_setting("chat_web_search", "maybe")
    assert ok is False
    assert "on" in err and "off" in err


def test_settings_page_renders_chat_web_search_when_present_in_db(client, monkeypatch):
    """Ключ з'являється в БД лише після міграції 009 (kbmcp-сторона) — сторінка
    має відрендерити його мітку/help/reco так само, як і будь-який інший
    ключ групи 'ai', щойно рядок з'явиться в settings."""
    _login(client)
    conn = _Conn([_settings_row("chat_web_search", "off")])
    monkeypatch.setattr(admin_app, "db", lambda: conn)

    response = client.get("/settings")
    assert response.status_code == 200
    html = response.text
    assert SETTING_META["chat_web_search"]["label"] in html
    assert "billed separately by Anthropic" in html
    assert "Suggested" in html and "fresher-than-archive" in html
