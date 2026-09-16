#!/usr/bin/env bash
set -euo pipefail

# Check worker liveness and readiness on the internal port and the public HTTPS endpoint.

INTERNAL_URL="${INTERNAL_URL:-http://127.0.0.1:8004}"
PUBLIC_URL="${PUBLIC_URL:-https://tts-api.aitransclips.com}"

check() {
	local url="$1"
	if curl -fsS --max-time 10 "${url}" >/dev/null 2>&1; then
		echo "OK   ${url}"
	else
		echo "FAIL ${url}" >&2
		return 1
	fi
}

status=0
check "${INTERNAL_URL}/health/live" || status=1
check "${INTERNAL_URL}/health/ready" || status=1
check "${PUBLIC_URL}/health/live" || status=1
check "${PUBLIC_URL}/health/ready" || status=1

exit "${status}"
