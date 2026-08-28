"""HTTP client for fetchers: rate limited, conditional, honest about blocks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
import psycopg

from netguard import MAX_REDIRECTS, SsrfBlocked, assert_public_url  # noqa: F401
from . import ratelimit
from .config import config

log = logging.getLogger(__name__)

class SourceBlocked(Exception):
    """403/429 from a source.

    Kept distinct from a transient failure because the response is different:
    Cloudflare blocking the datacenter IP is the known risk on Discourse forums
    and it needs a human (egress proxy contingency), not a retry.
    """


class FetchError(Exception):
    """Anything else that stopped us getting the content."""


@dataclass
class Response:
    status_code: int
    text: str
    headers: dict[str, str]
    not_modified: bool = False

    def json(self) -> Any:
        import json

        return json.loads(self.text)


class HttpClient:
    """One instance per run. Shares a connection pool and the DB-backed limiter."""

    def __init__(self, conn: psycopg.Connection):
        self._conn = conn
        self._client = httpx.Client(
            timeout=config.http_timeout_seconds,
            # ВИМКНЕНО навмисно: редіректи веде _get_checked, щоб перевірити
            # SSRF на кожному кроці. httpx із follow_redirects=True пройшов би
            # 302 на внутрішній хост повз будь-яку перевірку входу.
            follow_redirects=False,
            headers={
                "User-Agent": config.user_agent,
                "Accept-Encoding": "gzip, deflate",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _get_checked(self, url: str, headers: dict[str, str]) -> httpx.Response:
        """GET із перевіркою SSRF на КОЖНОМУ кроці редіректу.

        Редіректи ведемо вручну саме тому, що перевіряти лише перший URL
        безглуздо: `https://evil.example` віддає 302 на `http://n8n:5678/rest/`
        — і httpx із `follow_redirects=True` слухняно туди піде, повз будь-яку
        перевірку входу.
        """
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            assert_public_url(current)
            try:
                response = self._client.get(current, headers=headers)
            except httpx.HTTPError as exc:
                raise FetchError(f"GET {current} failed: {exc}") from exc

            # httpx зараховує 304 до `is_redirect` (той самий 3xx-діапазон),
            # хоча "Not Modified" не несе Location і нікуди не веде —
            # інакше тут KeyError на другому+ полінгу тієї самої URL.
            if not response.is_redirect or response.status_code == 304:
                return response
            if hop == MAX_REDIRECTS:
                raise FetchError(f"GET {url}: більше за {MAX_REDIRECTS} редіректів")
            # `.join()` розкриває відносний Location відносно поточного URL —
            # інакше `Location: /rest/` після зовнішнього хоста перевірявся б
            # як голий шлях без хоста.
            current = str(response.url.join(response.headers["Location"]))

        raise FetchError(f"GET {url}: цикл редіректів")  # недосяжно, для mypy

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        use_cache: bool = True,
    ) -> Response:
        """GET with If-None-Match / If-Modified-Since from http_cache.

        A 304 comes back as `not_modified=True` with empty text — callers treat
        it as "nothing new here", which is the cheap path for polling forums
        that mostly have not changed.
        """
        request_headers = dict(headers or {})
        if use_cache:
            cached = self._conn.execute(
                "SELECT etag, last_modified FROM http_cache WHERE url = %s", (url,)
            ).fetchone()
            if cached:
                if cached["etag"]:
                    request_headers["If-None-Match"] = cached["etag"]
                if cached["last_modified"]:
                    request_headers["If-Modified-Since"] = cached["last_modified"]

        ratelimit.acquire(self._conn, url)
        response = self._get_checked(url, request_headers)

        if response.status_code in (403, 429):
            raise SourceBlocked(
                f"GET {url} returned {response.status_code} "
                f"(retry-after={response.headers.get('Retry-After', 'n/a')})"
            )
        if response.status_code == 304:
            return Response(304, "", dict(response.headers), not_modified=True)
        if response.status_code >= 400:
            raise FetchError(f"GET {url} returned {response.status_code}")

        if use_cache and (
            response.headers.get("ETag") or response.headers.get("Last-Modified")
        ):
            self._conn.execute(
                """
                INSERT INTO http_cache (url, etag, last_modified, fetched_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (url) DO UPDATE
                    SET etag = EXCLUDED.etag,
                        last_modified = EXCLUDED.last_modified,
                        fetched_at = EXCLUDED.fetched_at
                """,
                (url, response.headers.get("ETag"), response.headers.get("Last-Modified")),
            )
            self._conn.commit()

        return Response(response.status_code, response.text, dict(response.headers))

    def post_json(
        self, url: str, payload: Any, *, headers: dict[str, str] | None = None
    ) -> Response:
        ratelimit.acquire(self._conn, url)
        assert_public_url(url)
        try:
            response = self._client.post(url, json=payload, headers=headers or {})
        except httpx.HTTPError as exc:
            raise FetchError(f"POST {url} failed: {exc}") from exc

        if response.status_code in (403, 429):
            raise SourceBlocked(f"POST {url} returned {response.status_code}")
        if response.status_code >= 400:
            raise FetchError(f"POST {url} returned {response.status_code}")
        return Response(response.status_code, response.text, dict(response.headers))
