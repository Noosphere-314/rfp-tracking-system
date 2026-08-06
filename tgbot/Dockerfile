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
RUN useradd --create-home --uid 10004 tgbot
USER tgbot

CMD ["python", "main.py"]
