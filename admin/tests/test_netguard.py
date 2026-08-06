"""SSRF-захист вихідних запитів (netguard.py).

Форма «Додати джерело» приймає довільний URL і одразу за ним ходить — це її
заявлена поведінка. Тести нижче фіксують межу, за яку вона не має виходити.

Останній тест — найважливіший: він перевіряє не сам URL, а РЕДІРЕКТ. Перевірка
лише початкового URL безглузда, бо `https://evil.example` віддає 302 на
`http://n8n:5678/rest/` — і клієнт із follow_redirects=True слухняно туди піде.
"""

from __future__ import annotations

import pytest

from netguard import SsrfBlocked, assert_public_url


# ── 1. Схема ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://forum.arbitrum.foundation",  # http, не https
        "file:///etc/passwd",
        "gopher://127.0.0.1:6379/_INFO",  # класика SSRF на Redis
        "ftp://example.com",
        "//evil.example/path",  # без схеми
    ],
)
def test_only_https_is_allowed(url):
    with pytest.raises(SsrfBlocked):
        assert_public_url(url)


# ── 2. Непублічні адреси ───────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/",  # loopback
        "https://localhost/",  # резолвиться в loopback
        "https://10.0.0.1/",  # приватна
        "https://192.168.1.1/",
        "https://172.16.0.1/",
        "https://169.254.169.254/latest/meta-data/",  # метадані хмари
        "https://0.0.0.0/",
        "https://[::1]/",  # IPv6 loopback
    ],
)
def test_private_and_reserved_addresses_are_blocked(url):
    with pytest.raises(SsrfBlocked):
        assert_public_url(url)


def test_internal_service_names_are_blocked():
    """Docker-мережа резолвить `n8n` і `postgres` у приватні адреси.

    Це і є головний сценарій: n8n тримає ВСІ креденшіали системи
    (Pipedrive, Anthropic, Telegram), а його REST відкритий усередині мережі.
    """
    for host in ("n8n", "postgres", "kbmcp", "admin"):
        with pytest.raises(SsrfBlocked):
            assert_public_url(f"https://{host}:5678/rest/login")


def test_public_host_passes():
    """Контроль на «тест зелений, бо блокує геть усе»."""
    assert_public_url("https://forum.arbitrum.foundation/c/rfp/16.json") is None


# ── 3. Редіректи — те, заради чого все це ──────────────────────────


def test_redirect_to_internal_host_is_blocked(monkeypatch):
    """Зовнішній хост віддає 302 на внутрішній: кожен крок має перевірятися.

    Тест б'є по HttpClient, а не по assert_public_url: саме там живе цикл
    редіректів, і саме там помилка була б непомітною — запит виглядав би як
    звичайний успішний фетч зовнішнього форуму.
    """
    import httpx

    from worker import http as worker_http

    hops = []

    class _FakeResponse:
        def __init__(self, url, redirect_to=None):
            self.url = httpx.URL(url)
            self.headers = {"Location": redirect_to} if redirect_to else {}
            self.status_code = 302 if redirect_to else 200
            self.text = "ok"

        @property
        def is_redirect(self):
            return bool(self.headers.get("Location"))

    class _FakeClient:
        def get(self, url, headers=None):
            hops.append(url)
            if "evil.example" in url:
                return _FakeResponse(url, redirect_to="http://n8n:5678/rest/login")
            return _FakeResponse(url)

        def close(self):
            pass

    client = worker_http.HttpClient.__new__(worker_http.HttpClient)
    client._client = _FakeClient()
    client._conn = None

    # Хост має резолвитися, інакше тест впаде на першому ж кроці не з тієї
    # причини: підміняємо резолвер, а не мережу.
    def _fake_getaddrinfo(host, port, **kwargs):
        if host == "evil.example":
            return [(2, 1, 6, "", ("93.184.216.34", port))]  # публічна
        raise OSError(f"unexpected host {host}")

    monkeypatch.setattr("netguard.socket.getaddrinfo", _fake_getaddrinfo)

    with pytest.raises(SsrfBlocked) as exc:
        client._get_checked("https://evil.example/feed", {})

    assert "n8n" in str(exc.value) or "http" in str(exc.value)
    assert hops == ["https://evil.example/feed"], "після 302 запиту бути не мало"
