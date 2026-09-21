# video-worker — download only (yt-dlp + douyin-downloader + f2). No torch, no ML.
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8005 \
    VIDEO_OUTPUT_DIR=/data/output \
    COOKIE_DIR=/data/cookies

# ffmpeg → stream merge;  curl → healthcheck;  git → pip install from a git URL
# (douyin-downloader is GitHub-only; the PyPI name is an unrelated 2018 package)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-douyin.txt ./
# `f2` pins httpx/pydantic/websockets to versions that conflict with the web
# stack, so it is installed with --no-deps after its real runtime deps.
# douyin-downloader is pulled from GitHub (PyPI's `douyin-downloader` is a
# different, abandoned package).
#
# Playwright/Chromium is installed ONLY for scripts/fetch_douyin_cookies.py,
# which bootstraps an anonymous Douyin jar when none exists. Nothing in the
# download path launches a browser.
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -r requirements-douyin.txt \
    && pip install --no-cache-dir --no-deps 'f2>=0.0.1' \
    && pip install --no-cache-dir \
        'douyin-downloader @ git+https://github.com/jiji262/douyin-downloader' \
    && playwright install --with-deps chromium

COPY . .

RUN mkdir -p /data/output /data/cookies

EXPOSE 8005

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8005/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8005"]
