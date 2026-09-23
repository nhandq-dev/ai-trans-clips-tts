"""Gemini error classification (plan/009 T2.5).

Only genuinely retryable failures may be retried. Quota/billing exhaustion is
permanent for the job's lifetime, so retrying it just delays the error report.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from errors import PermanentError, TransientError  # noqa: E402
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
    assert _classify_genai_error(_ApiError(message, 429)) is PermanentError


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
