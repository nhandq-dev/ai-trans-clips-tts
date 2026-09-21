"""HMAC request signing for the video worker.

Mirrors the TTS worker's scheme so one `HMAC_KEYS_JSON` rotation covers both
services, but with `X-Video-*` headers so a signature minted for one service can
never be replayed against the other.

Canonical string (newline-separated):
    METHOD, PATH_AND_QUERY, UNIX_TIMESTAMP, NONCE, HEX_SHA256_OF_BODY
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from threading import Lock

KEY_ID_HEADER = "X-Video-Key-Id"
TIMESTAMP_HEADER = "X-Video-Timestamp"
NONCE_HEADER = "X-Video-Nonce"
CONTENT_SHA256_HEADER = "X-Video-Content-SHA256"
SIGNATURE_HEADER = "X-Video-Signature"


class SignatureError(Exception):
    """Raised when a signed request fails verification."""


def canonical_string(
    method: str,
    path_and_query: str,
    timestamp: str,
    nonce: str,
    content_sha256: str,
) -> str:
    return "\n".join([method.upper(), path_and_query, timestamp, nonce, content_sha256])


def compute_signature(secret: str, canonical: str) -> str:
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def parse_keys(raw: str) -> dict[str, str]:
    """Parse `HMAC_KEYS_JSON` (`{"key-id": "secret"}`). Returns {} when unset/invalid."""
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items() if isinstance(v, str) and v}


class NonceStore:
    """In-memory TTL store of recently seen nonces.

    Replay resistance does not survive a process restart, which is an accepted
    trade-off for a single private worker instance.
    """

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._seen: dict[str, float] = {}
        self._lock = Lock()

    def check_and_record(self, nonce: str, now: float) -> bool:
        with self._lock:
            self._evict(now)
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            return True

    def _evict(self, now: float) -> None:
        cutoff = now - self._ttl
        for nonce in [n for n, ts in self._seen.items() if ts < cutoff]:
            del self._seen[nonce]


class SignatureVerifier:
    def __init__(
        self, keys: Mapping[str, str], max_skew_seconds: int, nonce_ttl_seconds: int
    ) -> None:
        self._keys = dict(keys)
        self._max_skew = max_skew_seconds
        self._nonces = NonceStore(nonce_ttl_seconds)

    @property
    def configured(self) -> bool:
        return bool(self._keys)

    def verify(
        self,
        *,
        method: str,
        path_and_query: str,
        headers: Mapping[str, str],
        body: bytes,
        now: float | None = None,
    ) -> None:
        if not self._keys:
            raise SignatureError("signing keys are not configured")

        key_id = headers.get(KEY_ID_HEADER)
        timestamp = headers.get(TIMESTAMP_HEADER)
        nonce = headers.get(NONCE_HEADER)
        provided_body_hash = headers.get(CONTENT_SHA256_HEADER)
        provided_signature = headers.get(SIGNATURE_HEADER)

        if (
            key_id is None
            or timestamp is None
            or nonce is None
            or provided_body_hash is None
            or provided_signature is None
        ):
            raise SignatureError("missing signature headers")

        secret = self._keys.get(key_id)
        if secret is None:
            raise SignatureError("unknown key id")

        try:
            request_time = int(timestamp)
        except ValueError as exc:
            raise SignatureError("invalid timestamp") from exc

        current_time = time.time() if now is None else now
        if abs(current_time - request_time) > self._max_skew:
            raise SignatureError("timestamp outside allowed skew")

        if not hmac.compare_digest(sha256_hex(body), provided_body_hash.lower()):
            raise SignatureError("body hash mismatch")

        canonical = canonical_string(method, path_and_query, timestamp, nonce, provided_body_hash)
        expected = compute_signature(secret, canonical)
        if not hmac.compare_digest(expected, provided_signature.lower()):
            raise SignatureError("signature mismatch")

        if not self._nonces.check_and_record(nonce, current_time):
            raise SignatureError("nonce already used")
