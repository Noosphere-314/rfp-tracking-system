# Кореневий контекст, як у worker/admin/mcp (див. коментар у worker/Dockerfile) —
# без цього `docker build -f <svc>/Dockerfile … .` в CI не однаковий для всіх
# образів, а COPY worker/requirements.txt у сусідніх Dockerfile-ах узагалі не
# резолвиться.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY tgbot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tgbot/main.py ./

# Полінг — не вхідний трафік, root тут не потрібен.
# /data — точка монтування named volume для інвайт-allowlist: якщо каталог не
# створити в образі З ПРАВИЛЬНИМ власником ДО USER, Docker створить його на
# першому монтуванні як root:root 0755 — і tgbot (uid 10004) мовчки не зможе
# зберігати стан (перевірено на живому образі; бот при цьому працює, лише
# логує OSError і втрачає інвайти на рестарті).
RUN useradd --create-home --uid 10004 tgbot \
    && mkdir -p /data && chown tgbot:tgbot /data
USER tgbot

CMD ["python", "main.py"]
