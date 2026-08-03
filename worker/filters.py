"""Free regex pre-filter, run before anything reaches the Claude API.

The patterns are edited by non-engineers through a form, so both the compile and
the match are treated as untrusted: compile failures quarantine the row, and
every match runs under a timeout (ReDoS guard, A4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg
import regex

from .alerts import alert
from .config import config

log = logging.getLogger(__name__)


@dataclass
class Keyword:
    id: int
    pattern: regex.Pattern
    kind: str


@dataclass
class KeywordSet:
    includes: list[Keyword]
    excludes: list[Keyword]

    @property
    def empty(self) -> bool:
        return not self.includes

    def matches(self, text: str) -> tuple[bool, str | None]:
        """Return (passes, matched_pattern).

        An exclude wins over an include: recurring forum furniture that happens
        to contain the word "grant" should not reach the classifier.
        """
        haystack = text.lower()

        for keyword in self.excludes:
            if _search(keyword, haystack):
                return False, None

        if self.empty:
            # No usable include patterns — let everything through rather than
            # silently delivering nothing. The quarantine alert already fired.
            return True, None

        for keyword in self.includes:
            if _search(keyword, haystack):
                return True, keyword.pattern.pattern

        return False, None


def _search(keyword: Keyword, haystack: str) -> bool:
    try:
        return keyword.pattern.search(haystack, timeout=config.regex_timeout_seconds) is not None
    except TimeoutError:
        # Catastrophic backtracking on real content. Disable it now; the item
        # is not worth losing the run over.
        alert(
            f"keyword #{keyword.id} `{keyword.pattern.pattern}` timed out and was "
            f"disabled — rewrite it in the keywords form",
            level="error",
        )
        return False


def load_keywords(conn: psycopg.Connection) -> KeywordSet:
    """Compile enabled patterns; quarantine and alert on the ones that won't.

    Validate-on-read (A4): a bad row added through the form must not be able to
    take down the hourly run.
    """
    rows = conn.execute(
        "SELECT id, pattern, kind, valid FROM keywords WHERE enabled ORDER BY id"
    ).fetchall()

    includes: list[Keyword] = []
    excludes: list[Keyword] = []
    newly_invalid: list[str] = []
    revalidated: list[int] = []

    for row in rows:
        try:
            compiled = regex.compile(row["pattern"], regex.IGNORECASE)
        except regex.error as exc:
            if row["valid"]:
                newly_invalid.append(f"#{row['id']} `{row['pattern']}` — {exc}")
                conn.execute(
                    "UPDATE keywords SET valid = false, invalid_reason = %s WHERE id = %s",
                    (str(exc), row["id"]),
                )
            continue

        if not row["valid"]:
            revalidated.append(row["id"])

        keyword = Keyword(id=row["id"], pattern=compiled, kind=row["kind"])
        (includes if row["kind"] == "include" else excludes).append(keyword)

    if revalidated:
        conn.execute(
            "UPDATE keywords SET valid = true, invalid_reason = NULL WHERE id = ANY(%s)",
            (revalidated,),
        )
    if newly_invalid or revalidated:
        conn.commit()
    if newly_invalid:
        alert(
            "keyword patterns quarantined (they will not filter anything until "
            "fixed):\n" + "\n".join(newly_invalid),
            level="error",
        )

    log.info("loaded %d include / %d exclude keywords", len(includes), len(excludes))
    return KeywordSet(includes=includes, excludes=excludes)
