"""Error taxonomy for the pipeline worker (plan/009 §5.4 + T2.5).

Transient vs permanent matters for tenacity: only TransientError is retried.
"""

from __future__ import annotations


class PipelineError(Exception):
    """Base for all pipeline errors."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TransientError(PipelineError):
    """Retryable: timeout, 429, 5xx, network blip."""


class PermanentError(PipelineError):
    """Not retryable: 400, 422, invalid input."""


class QuotaExhaustedError(PermanentError):
    """Gemini quota/billing exhausted for THIS model.

    Retrying the same model is pointless, but other models have their own budget,
    so the caller should move straight to the next model instead of failing the
    job or burning retries.
    """


# Concrete codes (plan/009 §5.4)
DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
NO_AUDIO_TRACK = "NO_AUDIO_TRACK"
GEMINI_FAILED = "GEMINI_FAILED"
GEMINI_NOT_CONFIGURED = "GEMINI_NOT_CONFIGURED"
TTS_FAILED = "TTS_FAILED"
ALIGN_FAILED = "ALIGN_FAILED"
SUBTITLE_DETECT_FAILED = "SUBTITLE_DETECT_FAILED"
MUX_FAILED = "MUX_FAILED"
TOO_LONG = "TOO_LONG"
QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
CANCELED = "CANCELED"
