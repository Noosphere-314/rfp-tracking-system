# Built from the repo root — так само, як admin/ і mcp/ (compose задає
# context: .). Один контекст на всі три образи означає, що CI може будувати їх
# однією командою `docker build -f <svc>/Dockerfile … .`, а не тримати окремий
# контекст на кожен сервіс (F3: саме через розбіжність джоб `images` падав).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worker /app/worker/

# Fetching is I/O-bound and polite by design; no reason to run as root.
RUN useradd --create-home --uid 10001 worker && chown -R worker /app
USER worker

ENTRYPOINT ["python", "-m", "worker.main"]
CMD ["run"]
