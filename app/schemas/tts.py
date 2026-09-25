from __future__ import annotations

from typing import Literal

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
    owner: int | None = Field(default=None, ge=1)
    # Requesting user id. Only used by the async path, where the API asserts the
    # poller owns the job it asks about.
    user_id: int | None = Field(default=None, ge=1)
    # Queue ordering: higher runs first (plan/014 P3.1). User plans use 10..100;
    # the video pipeline reserves 500 so its dubbing is never starved.
    priority: int = Field(default=0, ge=-1000, le=1000)
    # Billing tier; `free` counts against the shared Free-pool cap.
    tier: Literal["free", "paid"] | None = Field(default=None)


def normalize_format(value: str) -> str:
    return value.lower().lstrip(".")


def validate_tts_request(
    req: TTSRequest, *, custom_voice: bool = False, async_job: bool = False
) -> str:
    """Validate a request and return the normalized format.

    Set `custom_voice=True` when `req.voice` is a registered cloned voice so the
    catalog membership check is skipped. Set `async_job=True` for the queued path,
    which is not bound by the synchronous ceiling.

    Raises `TTSValidationError` for anything the caller can fix; the route maps that to a 400.
    """
    settings = get_settings()

    if not is_supported_language(req.language):
        raise TTSValidationError(f"unsupported language: {req.language!r}")

    fmt = normalize_format(req.format)
    if fmt not in ALLOWED_FORMATS:
        raise TTSValidationError("format must be 'mp3' or 'wav'")

    text_length = len(req.text)
    limit = settings.async_max_text_length if async_job else settings.sync_max_text_length
    if text_length > limit:
        mode = "asynchronous" if async_job else "synchronous"
        raise TTSValidationError(f"text exceeds the {mode} limit of {limit}")

    if custom_voice:
        if req.language.strip().lower().split("-")[0] != "vi":
            raise TTSValidationError("custom voices are only available for Vietnamese (vi)")
    elif req.voice and not is_valid_voice(req.voice, req.language):
        raise TTSValidationError(f"voice {req.voice!r} is not valid for language {req.language!r}")

    return fmt
