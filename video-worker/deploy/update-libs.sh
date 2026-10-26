#!/usr/bin/env bash
# Refresh the download libraries inside the running video-worker container.
# Invoked by video-worker-libs.service (systemd timer, every 2 days).
#
# `--no-deps` is deliberate: f2/douyin-downloader pin httpx/pydantic versions
# that conflict with the web stack, so only the top-level package is upgraded.
set -euo pipefail

COMPOSE_DIR="${COMPOSE_DIR:-/opt/video-worker}"
cd "$COMPOSE_DIR"

container_id="$(docker compose ps -q video-worker)"
if [[ -z "$container_id" ]]; then
  echo "video-worker container is not running" >&2
  exit 1
fi

docker exec "$container_id" pip install -U --no-cache-dir --no-deps \
  yt-dlp "f2==0.0.1.7"
docker exec "$container_id" pip install -U --no-cache-dir --no-deps \
  "douyin-downloader @ git+https://github.com/jiji262/douyin-downloader"
docker compose restart video-worker
echo "video-worker libs updated at $(date -Is)"
