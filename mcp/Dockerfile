FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY mcp/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp/server.py mcp/briefing.py ./

RUN useradd --create-home --uid 10003 kbmcp
USER kbmcp

EXPOSE 8000
CMD ["python", "server.py"]
