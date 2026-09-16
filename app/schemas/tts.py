from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.core.config import get_settings


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1)
    language: str = Field(default="vi", max_length=20)
    voice: str | None = Field(default=None, max_length=100)
    format: str = Field(default_factory=lambda: get_settings().tts_format)

    @field_validator("text")
    @classmethod
    def _within_max_length(cls, value: str) -> str:
        limit = get_settings().max_text_length
        if len(value) > limit:
            raise ValueError(f"text must be at most {limit} characters")
        return value
