from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["development", "staging", "production"]
LogLevel = Literal["debug", "info", "warning", "error"]


class Settings(BaseSettings):
    """Application settings sourced from environment variables and an optional `.env` file.

    Environment variable names map to field names case-insensitively, so `APP_ENV` maps to
    `app_env`, `SYNC_MAX_TEXT_LENGTH` to `sync_max_text_length`, and so on.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_env: AppEnv = "development"
    log_level: LogLevel = "info"
    host: str = "0.0.0.0"
    port: int = 8004
    disable_docs: bool = False
    docs_password: str = ""

    # TTS runtime
    # Technical ceiling for a single synchronous `/v1/tts` request: how much text the
    # single-lane worker can finish before the caller's timeout. This is a capability,
    # NOT a business limit — per-plan character caps live in the API
    # (`tts_max_chars_per_request`, plan/015 §5.3).
    sync_max_text_length: int = 2000
    # uvicorn process count (plan/014 Phase 2). Read by docker-entrypoint.sh; each
    # process loads its own model, so RAM scales with it while throughput does not
    # scale linearly (measured 1 lane = 37.5, 4 lanes = 90.2 chars/s on 10 cores).
    tts_workers: int = 1
    # Requests admitted per process before queueing. Total in-flight is
    # `tts_workers * tts_concurrency`, but only one synthesis runs per process.
    tts_concurrency: int = 2
    # Async job path (plan/014 Phase 1).
    # Ceiling for one async request. Unlike `sync_max_text_length` this is not a
    # capability bound — the job is decoupled from the HTTP request, so it can be
    # as large as the per-plan caps the API enforces.
    async_max_text_length: int = 30000
    # Redis backs the shared job store + queue. Required once `tts_workers > 1`,
    # because in-process job state is invisible to the other workers.
    redis_url: str | None = None
    tts_job_ttl_seconds: int = 86400
    # Shared ceiling on active Free-tier jobs (plan/015). Paid tiers are unaffected.
    tts_free_active_job_limit: int = 10
    # Start the in-process async job consumer. Disabled by tests, and by any
    # deployment that consumes the queue from a separate process.
    tts_jobs_consumer_enabled: bool = True
    tts_format: str = "mp3"
    tts_output_dir: Path = Path("tts_output")
    hf_home: Path | None = None
    ffmpeg_bin: str = ""
    vieneu_backend: str = "onnx"
    vieneu_default_voice: str = "Adam"
    vieneu_chunk_chars: int = 280
    edge_fallback_voice: str = "en-US-JennyNeural"
    edge_chunk_chars: int = 300

    # Custom (cloned) Vietnamese voices
    custom_voice_enabled: bool = True
    custom_voice_min_seconds: float = 8.0
    custom_voice_max_seconds: float = 10.0
    custom_voice_max: int = 3
    custom_voice_prefix: str = "custom-voices"
    ffprobe_bin: str = "ffprobe"

    # Object storage (S3-compatible; Cloudflare R2 in production)
    s3_endpoint: str = ""
    s3_region: str = "auto"
    s3_bucket: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_force_path_style: bool = True

    # Security
    hmac_keys_json: str = ""
    hmac_max_skew_seconds: int = 60
    hmac_nonce_ttl_seconds: int = 300
    request_max_body_bytes: int = 6 * 1024 * 1024

    # Operations
    request_id_header: str = "X-Request-Id"
    access_log_enabled: bool = True
    readiness_warmup: bool = False

    _hmac_keys: dict[str, str] = PrivateAttr(default_factory=dict)

    @field_validator("app_env", "log_level", mode="before")
    @classmethod
    def _lowercase(cls, value: object) -> object:
        """Accept case-insensitive enum values such as `PRODUCTION` or `INFO`."""
        return value.lower() if isinstance(value, str) else value

    @field_validator("hf_home", mode="before")
    @classmethod
    def _blank_hf_home_is_none(cls, value: object) -> object:
        """Treat an empty HF_HOME as unset instead of coercing to `Path('.')`."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("tts_output_dir", mode="before")
    @classmethod
    def _blank_output_dir_uses_default(cls, value: object) -> object:
        """Treat an empty TTS_OUTPUT_DIR as the default instead of `Path('.')`."""
        if isinstance(value, str) and not value.strip():
            return "tts_output"
        return value

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        self._hmac_keys = self._parse_hmac_keys()
        if self.is_production and not self._hmac_keys:
            raise ValueError("HMAC_KEYS_JSON is required when APP_ENV=production")
        if self.is_production and not self.docs_disabled and not self.docs_password:
            raise ValueError(
                "DOCS_PASSWORD is required when docs are enabled in production "
                "(set DOCS_PASSWORD or DISABLE_DOCS=true)"
            )
        if self.custom_voice_max_seconds <= self.custom_voice_min_seconds:
            raise ValueError("CUSTOM_VOICE_MAX_SECONDS must exceed CUSTOM_VOICE_MIN_SECONDS")
        if self.custom_voice_min_seconds <= 0:
            raise ValueError("CUSTOM_VOICE_MIN_SECONDS must be positive")
        s3_parts = (self.s3_bucket, self.s3_access_key_id, self.s3_secret_access_key)
        if any(s3_parts) and not all(s3_parts):
            raise ValueError(
                "S3_BUCKET, S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY must be set together"
            )
        return self

    def _parse_hmac_keys(self) -> dict[str, str]:
        raw = (self.hmac_keys_json or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"HMAC_KEYS_JSON must be valid JSON: {exc}") from exc
        if not isinstance(parsed, dict) or not parsed:
            raise ValueError("HMAC_KEYS_JSON must be a non-empty JSON object of {keyId: secret}")

        keys: dict[str, str] = {}
        for key_id, secret in parsed.items():
            if not isinstance(key_id, str) or not key_id:
                raise ValueError("HMAC_KEYS_JSON keys must be non-empty strings")
            if not isinstance(secret, str) or not secret:
                raise ValueError("HMAC_KEYS_JSON values must be non-empty strings")
            keys[key_id] = secret
        return keys

    @property
    def hmac_keys(self) -> dict[str, str]:
        """Accepted signing keys as a `{key_id: secret}` mapping."""
        return self._hmac_keys

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def s3_configured(self) -> bool:
        """True when an S3-compatible bucket and credentials are configured."""
        return bool(self.s3_bucket and self.s3_access_key_id and self.s3_secret_access_key)

    @property
    def docs_disabled(self) -> bool:
        """Docs are only disabled when explicitly requested.

        In production the docs stay reachable but are gated behind `DOCS_PASSWORD`
        (enforced in `_validate`), so they are never exposed without a password.
        """
        return self.disable_docs


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached for cheap repeated access; tests can call `reset_settings_cache()` after
    overriding environment variables.
    """
    return Settings()


def reset_settings_cache() -> None:
    """Clear the cached settings instance (primarily for tests)."""
    get_settings.cache_clear()
