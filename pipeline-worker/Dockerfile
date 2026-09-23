# pipeline-worker — translation pipeline (download → Gemini → TTS → subtitle → mux)
# 3.12 to match pyproject.toml (requires-python >=3.12, ruff target py312) and local dev.
# On <3.11 `asyncio.wait_for` raises asyncio.TimeoutError, which is NOT the builtin
# TimeoutError — a mismatch that once killed the scheduler silently.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8006 \
    WORK_DIR=/data/pipeline-output \
    CACHE_DIR=/data/pipeline-cache

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data/pipeline-output /data/pipeline-cache

EXPOSE 8006

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8006/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8006"]
