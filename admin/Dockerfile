# Built from the repo root (compose sets context: .) so the worker package is
# available too: the "add source" test-fetch runs the real fetcher, and the
# mock n8n endpoint reuses the same DB layer.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY worker/requirements.txt worker-requirements.txt
COPY admin/requirements.txt admin-requirements.txt
RUN pip install --no-cache-dir -r worker-requirements.txt -r admin-requirements.txt

COPY worker /app/worker
COPY admin /app/admin
COPY migrations /app/migrations

RUN useradd --create-home --uid 10002 admin && chown -R admin /app
USER admin

EXPOSE 8000
# --proxy-headers обов'язковий: без нього дефолт --forwarded-allow-ips=127.0.0.1,
# а peer-IP за Caddy — адреса контейнера (172.x), тож X-Forwarded-Proto
# ігнорується, request.url.scheme завжди "http" і сесійна cookie ніколи не стає
# Secure у проді (admin/auth.py, розділ 1.3).
#
# "*" тут прийнятний: єдине, що з цих заголовків виводиться, — прапорець Secure
# на cookie, і підміна X-Forwarded-Proto шкодить лише самому підмінювачу (його
# браузер перестане віддавати cookie по http). Троттл брутфорсу навмисно не
# залежить від IP, тож класичний обхід через XFF тут неможливий.
#
# --workers НЕ додавати: session_epoch кешується в пам'яті процесу
# (інваріант «один воркер», admin/auth.py).
CMD ["uvicorn", "admin.app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
