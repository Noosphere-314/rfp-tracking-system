"""Тест worker/http.py: HttpClient._get_checked() і власний redirect-цикл.

Контекст (прод, 2026-08-28): TechCrunch RSS почав падати з
`KeyError: 'Location'` на КОЖНОМУ прогоні, щойно в http_cache з'явився
ETag/Last-Modified для його URL. Причина — httpx зараховує 304 Not Modified
до `response.is_redirect` (той самий 3xx-діапазон, що й 301/302/307), хоча
"Not Modified" не несе Location і нікуди не веде. `_get_checked` веде
редіректи вручну (навмисно, заради SSRF-перевірки на кожному кроці — див.
коментар у самому файлі) і намагався прочитати `headers["Location"]` на
кожному "редіректі", включно з 304.

Тут HttpClient зібраний з реальним httpx.Client на MockTransport (не
фейк-чергою, як в інших worker/tests) — бо сам баг був у власному циклі
`_get_checked`, який фейк-клієнт з інших тестів обходить стороною.
"""

from __future__ import annotations

import httpx
import pytest

from worker.http import HttpClient


def _client_with_transport(handler) -> HttpClient:
    client = HttpClient.__new__(HttpClient)
    client._conn = None
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    return client


def test_get_checked_returns_304_without_location() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304, headers={}, request=request)

    client = _client_with_transport(handler)

    response = client._get_checked("https://example.com/feed", {})

    assert response.status_code == 304


def test_get_checked_still_follows_real_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(
                301, headers={"Location": "/new"}, request=request
            )
        return httpx.Response(200, text="ok", request=request)

    client = _client_with_transport(handler)

    response = client._get_checked("https://example.com/old", {})

    assert response.status_code == 200
    assert response.text == "ok"


def test_get_checked_rejects_redirect_without_location() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={}, request=request)

    client = _client_with_transport(handler)

    with pytest.raises(KeyError):
        client._get_checked("https://example.com/broken", {})
