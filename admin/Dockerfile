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
CMD ["uvicorn", "admin.app:app", "--host", "0.0.0.0", "--port", "8000"]
