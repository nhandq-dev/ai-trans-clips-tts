#!/usr/bin/env bash
set -euo pipefail

# Deploy a specific image tag to the tts-worker compose stack.
#
# Usage: deploy.sh <image-tag> [compose-dir]
#
# Records the previously deployed tag in releases/previous_tag so rollback.sh can
# return to it, and only reports success once /health/ready passes.

IMAGE_TAG="${1:?usage: deploy.sh <image-tag> [compose-dir]}"
COMPOSE_DIR="${2:-/opt/tts-worker}"
STATE_DIR="${COMPOSE_DIR}/releases"
CURRENT_TAG_FILE="${STATE_DIR}/current_tag"
PREVIOUS_TAG_FILE="${STATE_DIR}/previous_tag"
READY_URL="${READY_URL:-http://127.0.0.1:8004/health/ready}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-30}"
SLEEP_SECONDS="${SLEEP_SECONDS:-2}"

mkdir -p "${STATE_DIR}"
cd "${COMPOSE_DIR}"

# Retire the legacy host-venv service if it is still running: it binds :8004 and would
# prevent the container from starting. Idempotent once removed.
if systemctl list-unit-files 2>/dev/null | grep -q '^tts-worker.service'; then
	echo "Retiring legacy tts-worker.service"
	systemctl disable --now tts-worker.service || true
	rm -f /etc/systemd/system/tts-worker.service
	systemctl daemon-reload
fi

if [[ -f "${CURRENT_TAG_FILE}" ]]; then
	cp "${CURRENT_TAG_FILE}" "${PREVIOUS_TAG_FILE}"
fi

export IMAGE_TAG
echo "Deploying image tag: ${IMAGE_TAG}"

docker compose pull tts-worker
docker compose up -d --remove-orphans

for _ in $(seq 1 "${MAX_ATTEMPTS}"); do
	if curl -fsS "${READY_URL}" >/dev/null 2>&1; then
		printf '%s\n' "${IMAGE_TAG}" > "${CURRENT_TAG_FILE}"
		echo "Deploy succeeded: ${IMAGE_TAG}"
		exit 0
	fi
	sleep "${SLEEP_SECONDS}"
done

echo "Readiness check failed: ${READY_URL}" >&2
exit 1
