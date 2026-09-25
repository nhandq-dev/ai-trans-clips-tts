"""Per-job length cap (plan/015).

The API sends the plan's `videoDurationMaxPerJobSeconds`; the worker rejects a
longer source once it is local, so the message can quote the real length.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from errors import PermanentError  # noqa: E402
from pipeline import enforce_max_duration  # noqa: E402


def test_within_the_cap_is_allowed():
    enforce_max_duration(3599, 3600)
    enforce_max_duration(60, 120)


def test_exactly_the_cap_is_allowed():
    enforce_max_duration(3600, 3600)


def test_longer_than_the_cap_is_rejected_with_the_length():
    with pytest.raises(PermanentError) as excinfo:
        enforce_max_duration(4200, 3600)

    assert excinfo.value.code == "DURATION_EXCEEDED"
    assert "70.0 minutes" in excinfo.value.message
    assert "60 minute(s)" in excinfo.value.message


def test_no_cap_means_no_limit():
    enforce_max_duration(10_000, None)
    enforce_max_duration(10_000, 0)
