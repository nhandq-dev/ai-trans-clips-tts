"""Gemini error classification (plan/009 T2.5).

Only genuinely retryable failures may be retried. Quota/billing exhaustion is
permanent for the job's lifetime, so retrying it just delays the error report.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from errors import PermanentError, QuotaExhaustedError, TransientError  # noqa: E402
from gemini import _classify_genai_error  # noqa: E402


class _ApiError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@pytest.mark.parametrize(
    "message",
    [
        "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: generate_content_free_tier_requests",
        "You exceeded your current quota, please check your plan and billing details",
        "RESOURCE_EXHAUSTED: quota exceeded",
    ],
)
def test_quota_exhaustion_is_permanent(message):
    # A PermanentError subclass, so retries are skipped, but the model loop still
    # advances to the next model (each model has its own quota).
    assert _classify_genai_error(_ApiError(message, 429)) is QuotaExhaustedError
    assert issubclass(QuotaExhaustedError, PermanentError)


@pytest.mark.parametrize(
    "message,status",
    [
        ("429 Too Many Requests: rate limit", 429),
        ("503 UNAVAILABLE. This model is currently experiencing high demand.", 503),
        ("Deadline exceeded", None),
        ("500 Internal error", 500),
    ],
)
def test_transient_failures_stay_retryable(message, status):
    assert _classify_genai_error(_ApiError(message, status)) is TransientError


def test_bad_request_is_permanent():
    assert _classify_genai_error(_ApiError("400 INVALID_ARGUMENT", 400)) is PermanentError


def test_model_not_found_is_retryable_so_fallback_can_run():
    assert _classify_genai_error(_ApiError("models/x is not found for API version", 404)) is (
        TransientError
    )


def test_quota_on_one_model_falls_through_to_the_next(monkeypatch, tmp_path):
    """Regression: quota on the primary model used to end the job after 4 slow
    model attempts (~214 s measured). Each Gemini model has its own quota, so the
    loop must move straight on without retrying the exhausted one."""
    import asyncio

    import cache as cache_module
    import gemini
    from schemas import TranscriptionResult

    # The real cache dir is shared between runs, so a previous run would make the
    # cache-aside lookup return early and the model loop would never be entered.
    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path / "cache")

    audio = tmp_path / "a.flac"
    audio.write_bytes(b"fake-audio")

    monkeypatch.setattr(gemini, "DEFAULT_MODEL", "m1")
    monkeypatch.setattr(gemini, "FALLBACK_MODELS", ["m2", "m3"])
    # The key pool must be non-empty, and `_client` now takes the key it builds for.
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(gemini, "_client", lambda api_key: object())

    calls: list[str] = []

    async def fake_call_once(client, model, audio_bytes, prompt, mime_type="audio/flac"):  # noqa: ANN001, ARG001
        calls.append(model)
        if model in ("m1", "m2"):
            raise QuotaExhaustedError(gemini.GEMINI_FAILED, f"{model} out of quota")
        return TranscriptionResult(detected_language="en", segments=[])

    monkeypatch.setattr(gemini, "_call_once", fake_call_once)

    result = asyncio.run(gemini.transcribe_and_translate(audio, "en", "vi"))

    assert calls == ["m1", "m2", "m3"], "exhausted models must not be retried"
    assert result.detected_language == "en"
