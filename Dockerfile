# tts-worker — see app/ for source.
# 3.12 to match pyproject.toml. uv comes from its official image so the build
# never has to download it from PyPI (slow/flaky on constrained networks).
FROM ghcr.io/astral-sh/uv:0.12.21 AS uv
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/data/hf-cache \
    PATH="/app/.venv/bin:$PATH"

COPY --from=uv /uv /uvx /bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl espeak-ng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app
COPY docker-entrypoint.sh /app/docker-entrypoint.sh

RUN chmod +x /app/docker-entrypoint.sh \
    && useradd --create-home --uid 10001 worker \
    && mkdir -p /data/hf-cache /data/output \
    && chown -R worker:worker /data /app
USER worker

EXPOSE 8004

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8004/health/live || exit 1

CMD ["/app/docker-entrypoint.sh"]