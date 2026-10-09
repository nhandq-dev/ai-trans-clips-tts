"""Whisper ASR transcription (whisper-asr pipeline).

Tests the ASR layer without a real model/AUDIO: ``_split_long_segment``
heuristics and the ``transcribe_whisper`` wrapper's segment construction via a
stubbed Whisper model.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asr  # noqa: E402
from schemas import TranscriptionResult  # noqa: E402


def test_split_long_segment_keeps_short_segment():
    parts = asr._split_long_segment("hi there", 0.0, 2.0)
    assert parts == [("hi there", 0.0, 2.0)]


def test_split_long_segment_splits_on_punct():
    a = "First sentence. Second sentence! Third?"
    parts = asr._split_long_segment(a, 0.0, 20.0)
    assert len(parts) == 3
    # order preserved and covers the original duration
    assert parts[0][0] == "First sentence."
    assert parts[-1][0] == "Third?"
    assert abs(parts[-1][2] - 20.0) < 0.01


def test_split_long_segment_falls_back_when_no_punct():
    a = "x" * 300  # no punctuation -> keep whole
    parts = asr._split_long_segment(a, 0.0, 20.0)
    assert parts == [(a, 0.0, 20.0)]


def _seg(start, end, text):
    from schemas import Segment

    return Segment(start=start, end=end, source_text=text, target_text="")


def test_merge_segments_joins_close_fragments():
    segs = [
        _seg(0.0, 1.3, "这地方危险吗"),
        _seg(1.3, 3.5, "这是造价一个亿的俯瞬生命之环"),
        _seg(3.5, 4.7, "正常看没有危险"),
        _seg(4.7, 6.0, "但是你要是想爬上去"),
    ]
    merged = asr._merge_segments(segs)
    # 4 fragments within the 6s cap, close gaps -> one sentence
    assert len(merged) == 1
    assert (
        merged[0].source_text
        == "这地方危险吗 这是造价一个亿的俯瞬生命之环 正常看没有危险 但是你要是想爬上去"
    )
    assert merged[0].start == 0.0
    assert merged[0].end == 6.0


def test_merge_segments_respects_max_cap():
    segs = [_seg(0.0, 3.0, "a" * 10), _seg(3.2, 6.4, "b" * 10), _seg(6.6, 10.0, "c" * 10)]
    merged = asr._merge_segments(segs)
    # merging across the 6s cap is refused at the boundary -> all standalone
    for s in merged:
        assert s.end - s.start <= asr.MAX_MERGE_SECONDS + 0.01
    assert len(merged) == 3


def test_merge_segments_breaks_on_large_gap():
    segs = [_seg(0.0, 2.0, "first"), _seg(5.0, 7.0, "second")]
    merged = asr._merge_segments(segs)
    assert len(merged) == 2
    assert merged[0].source_text == "first"
    assert merged[1].source_text == "second"


def test_merge_segments_empty_and_single():
    assert asr._merge_segments([]) == []
    single = asr._merge_segments([_seg(1.0, 2.5, "only")])
    assert len(single) == 1
    assert single[0].source_text == "only"


class _FakeWhisperModel:
    """Minimal fake whose .transcribe yields a fixed ordered segment stream."""

    def __init__(self, segments, language="zh"):
        self._segs = segments
        self._lang = language

    def transcribe(self, *args, **kwargs):
        class _Gen:
            def __init__(self, segs):
                self._segs = segs

            def __iter__(self):
                return iter(self._segs)

        class _Info:
            pass

        info = _Info()
        info.language = self._lang
        return _Gen(self._segs), info


def test_transcribe_whisper_builds_ordered_segments(tmp_path, monkeypatch):
    class _Seg:
        def __init__(self, start, end, text):
            self.start = start
            self.end = end
            self.text = text

    fake = _FakeWhisperModel(
        [
            _Seg(0.0, 3.0, "line one"),
            _Seg(3.5, 7.5, "line two"),
            _Seg(8.0, 20.0, "long one here. And another."),
        ],
        language="zh",
    )
    monkeypatch.setattr(asr, "_model", fake)

    audio = tmp_path / "a.flac"
    audio.write_bytes(b"fake")

    result = asyncio.run(asr.transcribe_whisper(audio, "auto"))
    assert isinstance(result, TranscriptionResult)
    assert result.detected_language == "zh"
    # the long segment split on punctuation
    texts = [s.source_text for s in result.segments]
    assert "line one" in texts
    assert len(result.segments) >= 4
    # strictly chronological
    for a, b in zip(result.segments, result.segments[1:], strict=False):
        assert a.start < b.start or (a.start == b.start and a.end <= b.end)
    # target_text left empty for the translation step
    assert all(s.target_text == "" for s in result.segments)
    # diagnostic file written
    assert (tmp_path / "a.flac.whisper.json").exists()


def test_transcribe_whisper_passes_language_hint(tmp_path, monkeypatch):
    captured = {}

    class _Fake:
        def __init__(self):
            pass

        def transcribe(self, path, language, **kwargs):
            captured["language"] = language
            gen = iter([])
            return gen, type("I", (), {"language": "ja"})()

    monkeypatch.setattr(asr, "_model", _Fake())
    audio = tmp_path / "b.flac"
    audio.write_bytes(b"fake")
    asyncio.run(asr.transcribe_whisper(audio, "ja"))
    assert captured["language"] == "ja"


def test_transcribe_whisper_auto_uses_none(monkeypatch, tmp_path):
    captured = {}

    class _Fake:
        def __init__(self):
            pass

        def transcribe(self, path, language, **kwargs):
            captured["language"] = language
            return iter([]), type("I", (), {"language": "en"})()

    monkeypatch.setattr(asr, "_model", _Fake())
    audio = tmp_path / "c.flac"
    audio.write_bytes(b"fake")
    asyncio.run(asr.transcribe_whisper(audio, "auto"))
    assert captured["language"] is None
