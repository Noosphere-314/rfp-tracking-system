"""SSRF-перевірка вихідних URL. Чистий stdlib — навмисно.

Живе в корені, а не в `worker/`, бо потрібна трьом образам одразу (worker,
admin, kbmcp), а `worker/` тягне psycopg і решту залежностей, яких у kbmcp
немає. Один файл без імпортів поза stdlib копіюється куди завгодно.

Навіщо взагалі. Форма «Додати джерело» в дашборді і форма реєстрації форуму
в базі знань приймають довільний URL і одразу за ним ходять — це заявлена
поведінка (живий тест-фетч перед збереженням). Обидві форми стоять на
публічному домені за одним спільним паролем команди. Без перевірки нижче
будь-хто, хто має пароль, перетворює наш сервер на проксі до внутрішньої
мережі: `http://n8n:5678/rest/*` (усі креденшіали), `http://kbmcp:8000`,
`postgres:5432`, метадані хмари на `169.254.169.254`. Текст помилки при цьому
повертається в браузер, тобто відповідь видно — SSRF напівсліпий, не сліпий.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# Скільки редіректів дозволяємо пройти, перевіряючи кожен крок.
MAX_REDIRECTS = 5


class SsrfBlocked(Exception):
    """URL веде не в публічний інтернет.

    Окремий тип навмисно: це не збій мережі і не помилка джерела, а відмова за
    політикою. Її не ретраять і не карантинять — її показують людині.
    """


def assert_public_url(url: str) -> None:
    """Пропускає лише https на публічну IP-адресу. Інакше кидає SsrfBlocked.

    Перевіряються ВСІ адреси, які повернув резолвер: хост із записами і в
    публічну, і в приватну мережу інакше проходив би залежно від порядку
    відповіді DNS.

    Лишається вікно TOCTOU (DNS rebinding: резолв тут і резолв усередині httpx
    — різні запити, і між ними відповідь може змінитися). Закрити його повністю
    означає резолвити самому і йти на IP з підставленим `Host`, що ламає SNI і
    перевірку сертифіката. Свідомий компроміс: rebinding вимагає контрольованого
    DNS з нульовим TTL і точного таймінгу, тоді як пряме `http://n8n:5678`
    закривається цією перевіркою повністю.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        got = parsed.scheme or "без схеми"
        raise SsrfBlocked(f"дозволено тільки https (отримано {got}): {url[:80]}")

    host = parsed.hostname
    if not host:
        raise SsrfBlocked(f"в URL немає хоста: {url[:80]}")

    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SsrfBlocked(f"хост не резолвиться: {host} ({exc})") from exc

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local  # 169.254.169.254 — метадані хмари
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise SsrfBlocked(f"{host} веде на непублічну адресу {ip}")
