"""Subtitle blur-region detection helpers.

Covers the scan window (skip the opening titles, stop after ~15s) and the
first-line selection used to avoid blurring a two-line caption's second row.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import subtitles  # noqa: E402
from schemas import Segment, TranscriptionResult  # noqa: E402


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


def test_split_words_respects_limit():
    text = "mot hai ba bon nam sau bay tam chin muoi"
    chunks = subtitles._split_words(text, 12)
    assert chunks
    assert all(len(chunk) <= 12 for chunk in chunks)


def test_write_ass_uses_configured_font_and_splits_long_cues(tmp_path, monkeypatch):
    monkeypatch.setattr(subtitles, "FONT_SIZE", 18)
    monkeypatch.setattr(subtitles, "MAX_LINE_CHARS", 42)
    monkeypatch.setattr(subtitles, "MAX_LINES", 2)
    result = TranscriptionResult(
        detected_language="en",
        segments=[
            Segment(
                start=0.0,
                end=8.0,
                source_text="source",
                target_text=(
                    "Đây là một câu phụ đề rất dài để kiểm tra việc tự động "
                    "xuống dòng và chia nhỏ thành nhiều đoạn ngắn hơn."
                ),
            )
        ],
    )
    out = subtitles.write_ass(
        result,
        {"x": 0, "y": 900, "w": 1080, "h": 100},
        tmp_path / "out.ass",
        width=1080,
        height=1920,
    )
    content = out.read_text(encoding="utf-8")
    assert ",18," in next(line for line in content.splitlines() if line.startswith("Style:"))
    dialogues = [line for line in content.splitlines() if line.startswith("Dialogue:")]
    # The long caption is split into shorter cues instead of one screen-filling row.
    assert len(dialogues) >= 2
    for line in dialogues:
        text = line.rsplit(",", 1)[-1]
        assert text.count("\\N") <= 1
