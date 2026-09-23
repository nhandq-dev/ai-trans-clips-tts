#!/usr/bin/env bash
# Install the latest static ffmpeg/ffprobe on the VPS (plan/009 T0.1).
#
# The distro package (apt, Ubuntu 22.04) is ffmpeg 4.4.2, which lacks some of the
# filters the pipeline needs (`loudnorm`, `ass`, `boxblur`, `adelay`, `amix`,
# `atempo`, `delogo`). johnvansickle's static build is newer and self-contained.
#
# Usage:  sudo bash install-ffmpeg.sh
set -euo pipefail

DEST_DIR="${DEST_DIR:-/usr/local/bin}"
BUILD_URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

echo "==> downloading ${BUILD_URL}"
curl -fsSL "${BUILD_URL}" -o "${TMP_DIR}/ffmpeg.tar.xz"

echo "==> extracting"
tar -xJf "${TMP_DIR}/ffmpeg.tar.xz" -C "${TMP_DIR}"
SRC_DIR="$(find "${TMP_DIR}" -maxdepth 1 -type d -name 'ffmpeg-*-amd64-static' | head -n1)"
if [[ -z "${SRC_DIR}" ]]; then
  echo "ERROR: could not find extracted ffmpeg directory" >&2
  exit 1
fi

echo "==> installing to ${DEST_DIR}"
install -m 0755 "${SRC_DIR}/ffmpeg" "${DEST_DIR}/ffmpeg"
install -m 0755 "${SRC_DIR}/ffprobe" "${DEST_DIR}/ffprobe"

echo "==> version"
"${DEST_DIR}/ffmpeg" -version | head -n1

echo "==> verifying required filters"
REQUIRED_FILTERS=(adelay amix atempo loudnorm ass boxblur delogo overlay crop scale anullsrc)
FILTERS="$("${DEST_DIR}/ffmpeg" -hide_banner -filters 2>/dev/null)"
missing=0
for f in "${REQUIRED_FILTERS[@]}"; do
  if grep -qw "${f}" <<<"${FILTERS}"; then
    echo "  ok   ${f}"
  else
    echo "  MISS ${f}" >&2
    missing=1
  fi
done

if [[ "${missing}" -ne 0 ]]; then
  echo "ERROR: some required filters are missing" >&2
  exit 1
fi

echo "==> done. ffmpeg $( "${DEST_DIR}/ffmpeg" -version | head -n1 | awk '{print $3}') installed"
