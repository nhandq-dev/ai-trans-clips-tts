from __future__ import annotations

import pytest
from align import build_continuous_timeline


def test_places_clips_without_dead_air():
    spans = [(0.0, 2.0), (5.0, 7.0), (20.0, 22.0)]
    durations = [2.0, 1.5, 2.5]

    placements, _ = build_continuous_timeline(spans, durations, 30.0)

    assert placements[0] == (0.0, 2.0)
    gaps = [
        placements[i][0] - (placements[i - 1][0] + placements[i - 1][1])
        for i in range(1, len(placements))
    ]
    assert all(0.08 <= gap <= 0.35 + 1e-6 for gap in gaps)


def test_preserves_natural_pause_when_within_bounds():
    placements, _ = build_continuous_timeline(
        [(0.0, 2.0), (2.2, 4.0)], [2.0, 1.5], source_duration=10.0
    )

    assert placements[1][0] == pytest.approx(2.2)


def test_factor_matches_narration_length():
    spans = [(0.0, 10.0), (10.0, 20.0)]
    durations = [12.0, 12.0]

    placements, factor = build_continuous_timeline(spans, durations, 20.0)

    total = placements[-1][0] + placements[-1][1] + 0.3  # 0.3s tail pad
    assert factor == pytest.approx(total / 20.0)


def test_short_narration_does_not_speed_video_past_floor():
    placements, factor = build_continuous_timeline([(0.0, 1.0)], [1.0], 100.0)

    assert placements == [(0.0, 1.0)]
    assert factor == pytest.approx(0.8)


def test_merged_segment_internal_pause_never_becomes_dead_air():
    # A merged Whisper segment spans 20s, but the spoken text is only 3s long.
    placements, factor = build_continuous_timeline([(0.0, 20.0)], [3.0], 20.0)

    assert placements == [(0.0, 3.0)]
    assert factor == pytest.approx(0.8)


def test_missing_clip_durations_are_ignored():
    placements, _ = build_continuous_timeline([(0.0, 1.0), (2.0, 3.0)], [1.0], source_duration=10.0)

    assert len(placements) == 1


def test_empty_input():
    assert build_continuous_timeline([], [], 10.0) == ([], 1.0)
