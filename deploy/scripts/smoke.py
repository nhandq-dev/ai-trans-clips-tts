#!/usr/bin/env python3
"""Signed end-to-end smoke test for the TTS worker.

Validates HTTPS routing, HMAC signing, engine initialization, and real audio
generation. Intended to run on the VPS (it reads the HMAC key from the worker
env file), but any host with the key can run it.

Environment overrides:
  SMOKE_BASE_URL   (default https://tts-api.aitransclips.com)
  SMOKE_ENV_FILE   (default /opt/tts-worker/.env)
  SMOKE_KEY_ID / SMOKE_SECRET  (skip reading the env file)
  SMOKE_TIMEOUT    (default 300 seconds)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
import uuid

BASE_URL = os.environ.get("SMOKE_BASE_URL", "https://tts-api.aitransclips.com").rstrip("/")
ENV_FILE = os.environ.get("SMOKE_ENV_FILE", "/opt/tts-worker/.env")
TIMEOUT = float(os.environ.get("SMOKE_TIMEOUT", "300"))

KEY_ID = os.environ.get("SMOKE_KEY_ID", "")
SECRET = os.environ.get("SMOKE_SECRET", "")


def load_key() -> None:
    global KEY_ID, SECRET
    if KEY_ID and SECRET:
        return
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("HMAC_KEYS_JSON="):
                    raw = line.split("=", 1)[1].strip().strip("'\"")
                    keys = json.loads(raw)
                    KEY_ID, SECRET = next(iter(keys.items()))
                    return
    raise SystemExit(
        "No HMAC key configured. Set SMOKE_KEY_ID/SMOKE_SECRET or provide SMOKE_ENV_FILE."
    )


def sign(method: str, path: str, body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = "\n".join([method.upper(), path, timestamp, nonce, body_hash])
    signature = hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return {
        "X-TTS-Key-Id": KEY_ID,
        "X-TTS-Timestamp": timestamp,
        "X-TTS-Nonce": nonce,
        "X-TTS-Content-SHA256": body_hash,
        "X-TTS-Signature": signature,
    }


def call(method: str, path: str, body: bytes = b"", content_type: str | None = None):
    headers = sign(method, path, body)
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(
        BASE_URL + path, data=body or None, method=method, headers=headers
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.status, response.headers.get("Content-Type"), response.read()


def main() -> int:
    load_key()

    status, _, data = call("GET", "/v1/voices")
    if status != 200:
        print(f"FAIL GET /v1/voices -> {status}", file=sys.stderr)
        return 1
    catalog = json.loads(data)
    if not catalog.get("languages") or not catalog.get("voices"):
        print("FAIL GET /v1/voices -> empty catalog", file=sys.stderr)
        return 1
    print(f"OK   GET /v1/voices -> {status} ({len(catalog['languages'])} languages)")

    body = json.dumps(
        {"text": "Xin chào, đây là bài kiểm tra.", "language": "vi", "format": "mp3"}
    ).encode()
    status, content_type, data = call("POST", "/v1/tts", body=body, content_type="application/json")
    if status != 200 or content_type != "audio/mpeg" or len(data) < 1000:
        print(f"FAIL POST /v1/tts -> {status} {content_type} {len(data)} bytes", file=sys.stderr)
        return 1
    print(f"OK   POST /v1/tts -> {status} {content_type} ({len(data)} bytes)")

    print("smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
