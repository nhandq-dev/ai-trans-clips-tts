from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping
from threading import Lock

from app.core.config import Settings

KEY_ID_HEADER = "X-TTS-Key-Id"
TIMESTAMP_HEADER = "X-TTS-Timestamp"
NONCE_HEADER = "X-TTS-Nonce"
CONTENT_SHA256_HEADER = "X-TTS-Content-SHA256"
SIGNATURE_HEADER = "X-TTS-Signature"


class SignatureError(Exception):
    """Raised when a signed request fails verification."""


def canonical_string(
    method: str,
    path_and_query: str,
    timestamp: str,
    nonce: str,
    content_sha256: str,
) -> str:
    """Build the exact string the caller must sign.

    Format (newline-separated): METHOD, PATH_AND_QUERY, UNIX_TIMESTAMP, NONCE, BODY_SHA256.
    """
    return "\n".join([method.upper(), path_and_query, timestamp, nonce, content_sha256])


def compute_signature(secret: str, canonical: str) -> str:
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class NonceStore:
    """In-memory TTL store of recently seen nonces.

    Replay resistance does not survive a process restart, which is an accepted trade-off for a
    single private worker instance (see DEPLOYMENT.md).
    """

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._seen: dict[str, float] = {}
        self._lock = Lock()

    def check_and_record(self, nonce: str, now: float) -> bool:
        """Record `nonce` and return True, or return False if it was already seen."""
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
    """Verify HMAC-signed requests against the configured keys."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._nonces = NonceStore(settings.hmac_nonce_ttl_seconds)

    def verify(
        self,
        *,
        method: str,
        path_and_query: str,
        headers: Mapping[str, str],
        body: bytes,
        now: float | None = None,
    ) -> None:
        keys = self._settings.hmac_keys
        if not keys:
            raise SignatureError("signing keys are not configured")

        key_id = headers.get(KEY_ID_HEADER)
        timestamp = headers.get(TIMESTAMP_HEADER)
        nonce = headers.get(NONCE_HEADER)
        provided_body_hash = headers.get(CONTENT_SHA256_HEADER)
        provided_signature = headers.get(SIGNATURE_HEADER)

        if not all([key_id, timestamp, nonce, provided_body_hash, provided_signature]):
            raise SignatureError("missing signature headers")

        secret = keys.get(key_id)
        if secret is None:
            raise SignatureError("unknown key id")

        try:
            request_time = int(timestamp)
        except ValueError as exc:
            raise SignatureError("invalid timestamp") from exc

        current_time = time.time() if now is None else now
        if abs(current_time - request_time) > self._settings.hmac_max_skew_seconds:
            raise SignatureError("timestamp outside allowed skew")

        if not hmac.compare_digest(sha256_hex(body), provided_body_hash.lower()):
            raise SignatureError("body hash mismatch")

        canonical = canonical_string(method, path_and_query, timestamp, nonce, provided_body_hash)
        expected_signature = compute_signature(secret, canonical)
        if not hmac.compare_digest(expected_signature, provided_signature.lower()):
            raise SignatureError("signature mismatch")

        # Replay protection runs only after the signature is proven valid so that unauthenticated
        # callers cannot consume nonces.
        if not self._nonces.check_and_record(nonce, current_time):
            raise SignatureError("nonce replay detected")
