#!/usr/bin/env bash
set -euo pipefail

# Roll the tts-worker stack back to the previously deployed image tag.
#
# Usage: rollback.sh [compose-dir]

COMPOSE_DIR="${1:-/opt/tts-worker}"
STATE_DIR="${COMPOSE_DIR}/releases"
CURRENT_TAG_FILE="${STATE_DIR}/current_tag"
PREVIOUS_TAG_FILE="${STATE_DIR}/previous_tag"
READY_URL="${READY_URL:-http://127.0.0.1:8004/health/ready}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-30}"
SLEEP_SECONDS="${SLEEP_SECONDS:-2}"

if [[ ! -f "${PREVIOUS_TAG_FILE}" ]]; then
	echo "No previous tag recorded in ${PREVIOUS_TAG_FILE}; nothing to roll back to." >&2
	exit 1
fi

TARGET_TAG="$(cat "${PREVIOUS_TAG_FILE}")"
cd "${COMPOSE_DIR}"
export IMAGE_TAG="${TARGET_TAG}"
echo "Rolling back to image tag: ${TARGET_TAG}"

docker compose pull tts-worker
docker compose up -d --remove-orphans

for _ in $(seq 1 "${MAX_ATTEMPTS}"); do
	if curl -fsS "${READY_URL}" >/dev/null 2>&1; then
		printf '%s\n' "${TARGET_TAG}" > "${CURRENT_TAG_FILE}"
		echo "Rollback succeeded: ${TARGET_TAG}"
		exit 0
	fi
	sleep "${SLEEP_SECONDS}"
done

echo "Rollback readiness check failed: ${READY_URL}" >&2
exit 1
