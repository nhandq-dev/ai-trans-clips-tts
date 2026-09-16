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
    `app_env`, `MAX_TEXT_LENGTH` to `max_text_length`, and so on.
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

    # TTS runtime
    max_text_length: int = 5000
    sync_max_text_length: int = 2000
    tts_concurrency: int = 2
    tts_format: str = "mp3"
    tts_output_dir: Path = Path("tts_output")
    hf_home: Path | None = None
    ffmpeg_bin: str = ""
    vieneu_backend: str = "onnx"
    vieneu_default_voice: str = "Adam"
    vieneu_chunk_chars: int = 280
    edge_fallback_voice: str = "en-US-JennyNeural"
    edge_chunk_chars: int = 300

    # Security
    hmac_keys_json: str = ""
    hmac_max_skew_seconds: int = 60
    hmac_nonce_ttl_seconds: int = 300
    request_max_body_bytes: int = 2 * 1024 * 1024

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
        if self.sync_max_text_length > self.max_text_length:
            raise ValueError("SYNC_MAX_TEXT_LENGTH cannot exceed MAX_TEXT_LENGTH")
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
    def docs_disabled(self) -> bool:
        """Docs are disabled in production or when explicitly requested."""
        return self.disable_docs or self.is_production


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
