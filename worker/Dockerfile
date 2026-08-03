FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/worker/

# Fetching is I/O-bound and polite by design; no reason to run as root.
RUN useradd --create-home --uid 10001 worker && chown -R worker /app
USER worker

ENTRYPOINT ["python", "-m", "worker.main"]
CMD ["run"]
