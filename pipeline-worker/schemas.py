"""Pydantic schemas for Gemini structured output and internal segments."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Segment(BaseModel):
    start: float = Field(..., ge=0, description="seconds")
    end: float = Field(..., ge=0, description="seconds")
    source_text: str = Field(..., min_length=1)
    # Empty until translation fills it (Whisper ASR produces source only).
    target_text: str = Field(default="", description="translation, filled later")


class TranscriptionResult(BaseModel):
    detected_language: str = Field(..., min_length=2, max_length=10)
    segments: list[Segment] = Field(default_factory=list)
    # Token accounting for the job (summed across chunks). `exclude=True` keeps them
    # out of both the Gemini response schema and the on-disk cache.
    input_tokens: int = Field(default=0, exclude=True)
    output_tokens: int = Field(default=0, exclude=True)


class TTSClip(BaseModel):
    segment_index: int
    text: str
    language: str
    voice: str
    engine: str
