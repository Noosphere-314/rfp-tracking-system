"""Тести `admin.app.diagnose_error` / DIAG_RULES (розділ C: детермінована
діагностика помилок на /runs — «що робити з помилками», без AI, без БД).

Без БД і без мережі: `diagnose_error` — чиста функція `str -> str` (текст
помилки з worker_runs.detail['failures'] -> diag-ключ). Імпорт test_auth
ПЕРШИМ — той самий трюк, що й в інших файлах тут: env для DASHBOARD_PASSWORD
etc. виставляється до першого імпорту admin.app.
"""

from __future__ import annotations

from admin.tests.test_auth import client  # noqa: E402,F401

from admin.app import DIAG_ADVICE_EN, DIAG_FALLBACK, DIAG_RULES, diagnose_error  # noqa: E402


# ── Мапа "текст помилки → очікуваний diag-ключ" (розділ C: перелік із задачі,
# ті самі формати рядків, що пише worker/pipeline.py та worker/http.py) ──

CASES = [
    ("Optimism Forum: BLOCKED — GET https://x returned 429 (retry-after=30)", "rate_limited"),
    ("Some Source: FetchError — GET https://x failed: rate limit exceeded", "rate_limited"),
    ("Arbitrum RFPs: FetchError — GET https://x returned 401", "auth"),
    ("Arbitrum RFPs: BLOCKED — GET https://x returned 403 (retry-after=n/a)", "auth"),
    ("Old Forum: FetchError — GET https://x returned 404", "not_found"),
    ("Slow Source: FetchError — GET https://x failed: ConnectTimeout", "timeout"),
    ("Slow Source: FetchError — request timeout after 30s", "timeout"),
    ("Dead Domain: FetchError — GET https://x failed: [Errno -2] Name or service not known (gaierror)", "dns"),
    ("Dead Domain: FetchError — nodename nor servname provided, or not known", "dns"),
    ("Internal Probe: SsrfBlocked — x.example веде на непублічну адресу 10.0.0.5", "ssrf"),
    ("Weird API: FetchError — json.decoder.JSONDecodeError: Expecting value: line 1 column 1", "format"),
    ("Weird API: ValueError — unexpected format in response", "format"),
    ("Ghost Source: quarantined after 5 consecutive failures", "quarantined"),
    ("Totally Unrelated Source: KeyError — 'topic_id'", DIAG_FALLBACK),
]


def test_diag_rules_mapping_table():
    """Кожен рядок CASES — окрема перевірка «текст помилки → правильний
    diag-ключ», перше правило, що збіглося, перемагає (порядок DIAG_RULES)."""
    for text, expected_key in CASES:
        assert diagnose_error(text) == expected_key, text


def test_diag_fallback_when_nothing_matches():
    assert diagnose_error("completely generic message with no known signature") == DIAG_FALLBACK


def test_diag_rate_limit_wins_over_generic_timeout_wording_when_both_present():
    """429 — специфічніше за просте слово "timeout", яке іноді трапляється в
    тому самому повідомленні (наприклad, "retry-after" контекст) — порядок
    правил (429 перед timeout) має віддавати перевагу rate_limited."""
    text = "Source X: BLOCKED — GET https://x returned 429, timeout waiting for retry"
    assert diagnose_error(text) == "rate_limited"


def test_diag_advice_en_has_an_entry_for_every_rule_key_and_the_fallback():
    """Кожен ключ, який може повернути diagnose_error (усі DIAG_RULES + сам
    DIAG_FALLBACK), має відповідний запис у DIAG_ADVICE_EN — інакше шаблон
    runs.html впав би на KeyError у DIAG_ADVICE_EN[dkey]."""
    all_keys = {key for _, key in DIAG_RULES} | {DIAG_FALLBACK}
    assert all_keys <= set(DIAG_ADVICE_EN)
    for advice in DIAG_ADVICE_EN.values():
        assert advice and isinstance(advice, str)


def test_diag_key_is_a_registered_jinja_filter():
    """runs.html викликає `f | diag_key` — переконуємось, що фільтр
    зареєстрований і вказує саме на diagnose_error, а не загубився при
    рефакторингу templates.env.filters."""
    from admin.app import templates

    assert templates.env.filters["diag_key"] is diagnose_error
