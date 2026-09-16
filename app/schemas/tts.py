from __future__ import annotations

from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.services.voice_catalog import is_supported_language, is_valid_voice

ALLOWED_FORMATS = ("mp3", "wav")


class TTSValidationError(ValueError):
    """A TTS request failed business validation."""


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1)
    language: str = Field(default="vi", max_length=20)
    voice: str | None = Field(default=None, max_length=100)
    format: str = Field(default_factory=lambda: get_settings().tts_format)


def normalize_format(value: str) -> str:
    return value.lower().lstrip(".")


def validate_tts_request(req: TTSRequest) -> str:
    """Validate a request and return the normalized format.

    Raises `TTSValidationError` for anything the caller can fix; the route maps that to a 400.
    """
    settings = get_settings()

    if not is_supported_language(req.language):
        raise TTSValidationError(f"unsupported language: {req.language!r}")

    fmt = normalize_format(req.format)
    if fmt not in ALLOWED_FORMATS:
        raise TTSValidationError("format must be 'mp3' or 'wav'")

    text_length = len(req.text)
    if text_length > settings.max_text_length:
        raise TTSValidationError(f"text exceeds the maximum length of {settings.max_text_length}")
    if text_length > settings.sync_max_text_length:
        raise TTSValidationError(
            f"text exceeds the synchronous limit of {settings.sync_max_text_length}"
        )

    if req.voice and not is_valid_voice(req.voice, req.language):
        raise TTSValidationError(f"voice {req.voice!r} is not valid for language {req.language!r}")

    return fmt
