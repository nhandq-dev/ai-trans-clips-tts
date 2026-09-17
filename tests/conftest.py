from __future__ import annotations

import hashlib
import hmac
import os
import time
import uuid

import pytest

# Configure the app deterministically before any test module imports it. Real environment
# variables take precedence over a developer's `.env`, so these win.
os.environ["APP_ENV"] = "development"
os.environ["HMAC_KEYS_JSON"] = '{"test-key":"test-secret"}'
os.environ.setdefault("HMAC_MAX_SKEW_SECONDS", "60")
os.environ.setdefault("HMAC_NONCE_TTL_SECONDS", "300")
os.environ.setdefault("MAX_TEXT_LENGTH", "5000")
os.environ.setdefault("SYNC_MAX_TEXT_LENGTH", "2000")
os.environ.setdefault("TTS_CONCURRENCY", "1")
os.environ.setdefault("ACCESS_LOG_ENABLED", "false")

TEST_KEY_ID = "test-key"
TEST_SECRET = "test-secret"


def sign(
    method: str,
    path: str,
    body: bytes = b"",
    *,
    key_id: str = TEST_KEY_ID,
    secret: str = TEST_SECRET,
    timestamp: float | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Build the signed headers the worker expects."""
    ts = str(int(timestamp if timestamp is not None else time.time()))
    n = nonce or uuid.uuid4().hex
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = "\n".join([method.upper(), path, ts, n, body_hash])
    signature = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return {
        "X-TTS-Key-Id": key_id,
        "X-TTS-Timestamp": ts,
        "X-TTS-Nonce": n,
        "X-TTS-Content-SHA256": body_hash,
        "X-TTS-Signature": signature,
    }


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def signer():
    return sign
