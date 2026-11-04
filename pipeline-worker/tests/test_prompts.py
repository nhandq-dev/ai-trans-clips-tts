"""Translation prompt content: localization (currency, units, dates)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gemini  # noqa: E402


def test_vietnamese_prompts_localize_currency_and_units():
    prompts = (
        gemini._prompt(target_language="vi"),
        gemini._prompt_translate(target_language="vi"),
    )
    for prompt in prompts:
        assert "Localization (CRITICAL)" in prompt
        assert "VND" in prompt
        assert "°C" in prompt
        assert "exchange rate" in prompt


def test_other_targets_localize_without_vnd_specifics():
    prompt = gemini._prompt_translate(target_language="en")
    assert "Localization (CRITICAL)" in prompt
    assert "VND" not in prompt
