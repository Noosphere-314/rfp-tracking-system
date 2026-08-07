"""Тести `admin.app.md_lite` — безпечного підмножина-markdown для brief.html
(розділ 4.8, розширення «readable + lifecycle»).

Без БД і без мережі: md_lite — чиста функція `str -> Markup`. Імпорт
`admin.tests.test_auth` ПЕРШИМ — той самий трюк, що й у test_chat.py: він
виставляє env (DASHBOARD_PASSWORD, SESSION_SECRET, …) ДО того, як
`admin.app` (через `admin.auth`) читає їх на імпорті й падає fail-fast без
пароля. Якщо цей файл запустити одиночно (`pytest admin/tests/test_md_lite.py`),
імпорт test_auth раніше за admin.app лишається обов'язковим.
"""

from __future__ import annotations

from admin.tests.test_auth import client  # noqa: E402,F401

from admin.app import md_lite  # noqa: E402


def _html(text: str) -> str:
    return str(md_lite(text))


# ── Безпека: escape-FIRST означає, що сирий HTML фізично не може пройти ──


def test_script_tag_is_inert_not_executable():
    out = _html("hello <script>alert(1)</script> world")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_javascript_href_in_markdown_link_does_not_become_a_link():
    out = _html("[click me](javascript:alert(1))")
    assert "<a " not in out
    # Текст лишається читабельним escaped-рядком, а не зникає мовчки.
    assert "click me" in out
    assert "javascript:alert(1)" in out or "javascript:alert(1))" in out


def test_javascript_bare_url_is_not_autolinked():
    out = _html("see javascript:alert(document.cookie) for details")
    assert "<a " not in out


def test_data_uri_link_does_not_become_a_link():
    out = _html("[open](data:text/html,<script>alert(1)</script>)")
    assert "<a " not in out
    assert "<script>" not in out


def test_raw_html_attribute_injection_is_escaped():
    out = _html('<img src=x onerror=alert(1)>')
    assert "<img" not in out
    assert "onerror" in out  # лишається текстом, не атрибутом
    assert "&lt;img" in out


def test_plain_ampersand_and_quotes_are_escaped():
    out = _html("Terms & \"conditions\" apply")
    assert "&amp;" in out
    assert "&quot;" in out or "&#34;" in out


# ── Формат: заголовки, жирний/курсив, списки, посилання, абзаци ──────────


def test_h3_heading():
    out = _html("### Overview")
    assert "<h3>Overview</h3>" in out


def test_h2_heading_also_maps_to_h3():
    """`##` — реальний формат basic-рівня kbmcp (mcp/briefing.py:
    `## KB brief: {ecosystem} — {title}`), рівно один на бріф, ніколи не
    змішується з `###` в одному документі — обидва мапляться в один <h3>
    (розділ 4.8)."""
    out = _html("## KB brief: Optimism — Some RFP")
    assert "<h3>KB brief: Optimism — Some RFP</h3>" in out


def test_single_hash_is_left_as_paragraph_text():
    out = _html("# Not a heading")
    assert "<h1>" not in out
    assert "# Not a heading" in out


def test_bold_and_italic():
    out = _html("**bold text** and *italic text*")
    assert "<strong>bold text</strong>" in out
    assert "<em>italic text</em>" in out


def test_underscore_italic():
    """`_text_` — реальний формат kbmcp (mcp/briefing.py, футер кожного
    basic-бріфу: `_Auto-generated from the forum archive (…)_`)."""
    out = _html("_Auto-generated from the forum archive._")
    assert "<em>Auto-generated from the forum archive.</em>" in out


def test_underscore_italic_does_not_fire_inside_a_snake_case_identifier():
    """Регресія, знайдена наживо на docker compose: РЕАЛЬНИЙ футер kbmcp
    містить `ANTHROPIC_API_KEY` усередині `_..._`-речення. Без межі слова
    навколо underscore-роздільника `ANTHROPIC_API_KEY` сама стає парою
    підкреслень і розвалюється на `ANTHROPIC<em>API</em>KEY` — ідентифікатор
    не можна розрізати навпіл. Правильна, безпечна поведінка тут — узагалі
    не рендерити цей конкретний `_..._` (лишити текст як є), а не зламати
    середину слова."""
    out = _html(
        "Set _ANTHROPIC_API_KEY_ in the environment."
    )
    assert "<em>API</em>" not in out
    assert "ANTHROPIC_API_KEY" in out


def test_bullet_list():
    out = _html("- first\n- second\n- third")
    assert "<ul><li>first</li><li>second</li><li>third</li></ul>" in out


def test_https_markdown_link_renders_with_target_blank_and_noopener():
    out = _html("[the forum](https://gov.optimism.io/t/123)")
    assert (
        '<a href="https://gov.optimism.io/t/123" target="_blank" rel="noopener">'
        "the forum</a>" in out
    )


def test_http_markdown_link_is_not_special_cased_only_https_is():
    """Спец. `[label](url)`-синтаксис приймає лише https:// (розділ 4.8):
    мітка "old link" не стає текстом посилання для http://. (Голий
    http://-текст усередині дужок усе одно може автопосилатись окремим
    механізмом — bare-URL autolink працює для http/https однаково, це інший,
    навмисно ширший шлях.) """
    out = _html("[old link](http://example.com)")
    assert ">old link</a>" not in out
    assert "old link" in out


def test_bare_url_autolink():
    out = _html("see https://example.com/x for details")
    assert '<a href="https://example.com/x" target="_blank" rel="noopener">https://example.com/x</a>' in out


def test_paragraphs_separated_by_blank_line():
    out = _html("first paragraph\n\nsecond paragraph")
    assert "<p>first paragraph</p>" in out
    assert "<p>second paragraph</p>" in out


def test_consecutive_lines_within_a_paragraph_join_with_br():
    out = _html("line one\nline two")
    assert "<p>line one<br>line two</p>" in out


def test_no_images_rendered_even_with_markdown_image_syntax():
    """`![alt](https://...)` — image markdown НЕ підтримується (розділ 4.8):
    `!` лишається літеральним текстом перед посиланням, ніякого <img>."""
    out = _html("![alt text](https://example.com/pic.png)")
    assert "<img" not in out


def test_empty_and_none_input_returns_empty_markup():
    assert str(md_lite("")) == ""
    assert str(md_lite(None)) == ""


def test_full_brief_shape_end_to_end():
    text = (
        "### Overview\n"
        "This ecosystem runs **frequent** grant rounds.\n\n"
        "### Sources\n"
        "- forum thread: https://gov.optimism.io/t/1\n"
        "- [snapshot vote](https://snapshot.org/#/opcollective.eth)\n"
    )
    out = _html(text)
    assert out.count("<h3>") == 2
    assert "<strong>frequent</strong>" in out
    assert "<ul>" in out
    assert 'href="https://gov.optimism.io/t/1"' in out
    assert 'href="https://snapshot.org/#/opcollective.eth"' in out


def test_basic_tier_kbmcp_shape_end_to_end():
    """Форма реального виходу mcp/briefing.py::_basic_brief — перевірено на
    живому /briefs у docker compose (розділ 4.8): без цього тесту `##`
    (не `###`) і `_italics_` мовчки лишалися б literal-текстом на сторінці,
    саме так і сталося при першому проході — побачено наживо, не вигадано."""
    text = (
        "## KB brief: Optimism — Grant tracker RFP\n\n"
        "**Similar discussions in the governance forum:**\n"
        "- [Working Constitution](https://gov.optimism.io/t/1) — Get Started, "
        "628 posts, last activity 2026-01\n\n"
        "**Most active forum voices (last 180 days):**\n"
        "- MconnectDAO — 28 posts, last seen 2026-07-21\n\n"
        "_Auto-generated from the forum archive (keyword tier — set "
        "ANTHROPIC_API_KEY for analyst-grade briefs)._"
    )
    out = _html(text)
    assert "<h3>KB brief: Optimism — Grant tracker RFP</h3>" in out
    assert "<strong>Similar discussions in the governance forum:</strong>" in out
    assert '<a href="https://gov.optimism.io/t/1" target="_blank" rel="noopener">Working Constitution</a>' in out
    # Футер лишається БЕЗ <em> — див. test_underscore_italic_does_not_fire_
    # inside_a_snake_case_identifier: ANTHROPIC_API_KEY усередині цього ж
    # речення не дає жодному underscore-роздільнику знайти пару, тож увесь
    # `_..._` навколо лишається літеральним текстом — безпечніше, ніж
    # розрізати ідентифікатор навпіл.
    assert "ANTHROPIC_API_KEY" in out
    assert "<em>API</em>" not in out
    assert "##" not in out  # заголовок споживається повністю, решітки не лишається
    assert out.count("<ul>") == 2
