"""Subtitle blur-region detection helpers.

Covers the scan window (skip the opening titles, stop after ~15s) and the
first-line selection used to avoid blurring a two-line caption's second row.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import subtitles  # noqa: E402


def test_scan_window_skips_opening_and_stops_early(monkeypatch):
    monkeypatch.setattr(subtitles, "OCR_START_SECONDS", 5.0)
    monkeypatch.setattr(subtitles, "OCR_MAX_DURATION", 15.0)
    assert subtitles._scan_window(60.0) == (5.0, 15.0)


def test_scan_window_short_clip_starts_at_zero(monkeypatch):
    monkeypatch.setattr(subtitles, "OCR_START_SECONDS", 5.0)
    monkeypatch.setattr(subtitles, "OCR_MAX_DURATION", 15.0)
    assert subtitles._scan_window(4.0) == (0.0, 4.0)


def test_pick_first_line_ignores_second_line():
    # Two-line caption: line 1 sits at y~810, line 2 at y~870.
    boxes = [
        [100, 400, 790, 830],
        [100, 400, 790, 830],
        [100, 400, 850, 890],
        [100, 400, 850, 890],
    ]
    picked = subtitles._pick_first_line(boxes)
    assert picked
    assert all(b[3] <= 830 for b in picked)


def test_pick_first_line_prefers_top_line_even_with_fewer_hits():
    # The second line is detected more often but must not win.
    boxes = [
        [100, 400, 790, 830],
        [100, 400, 850, 890],
        [100, 400, 850, 890],
        [100, 400, 850, 890],
        [100, 400, 850, 890],
    ]
    picked = subtitles._pick_first_line(boxes)
    assert picked
    assert all(b[3] <= 830 for b in picked)
