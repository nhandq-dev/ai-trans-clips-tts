"""Dub timing helpers (plan/009 T2.2).

The default path lays the TTS clips out as one continuous track: each clip is
placed right after the previous one plus a short pause (clamped so a natural
silence in the source can never leave a multi-second hole). ``to_wav`` trims the
lead/trail silence TTS engines add, so the measured length is real speech.

``fit_to_slot`` is kept for callers that still want a clip stretched to a fixed
slot (legacy jobs/tests); the pipeline no longer uses it for dub playback.

Output is 48 kHz mono wav for later mux.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from errors import ALIGN_FAILED, PermanentError, TransientError

FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "ffprobe")

TRIM_SILENCE = os.getenv("ALIGN_TRIM_SILENCE", "1").strip().lower() not in ("0", "false", "no")
SILENCE_THRESHOLD_DB = os.getenv("ALIGN_SILENCE_THRESHOLD_DB", "-45")
SILENCE_GUARD_SECONDS = float(os.getenv("ALIGN_SILENCE_GUARD_SECONDS", "0.05"))
ALIGN_MIN_TEMPO = float(os.getenv("ALIGN_MIN_TEMPO", "0.5"))
ALIGN_MAX_TEMPO = float(os.getenv("ALIGN_MAX_TEMPO", "2.0"))
# When the clip is much shorter than its slot, never pad a one-liner into a
# minute of silence: cap the padded length at the slowed clip plus a small buffer.
SLOW_PAD_BUFFER_SECONDS = float(os.getenv("ALIGN_SLOW_PAD_BUFFER_SECONDS", "0.6"))

# Continuous dub timeline (default path). Clips are placed back-to-back; the
# pause between two lines is the source pause clamped to this range, so a short
# translation (or a long silence in the source) can never become dead air.
ALIGN_MIN_GAP_SECONDS = float(os.getenv("ALIGN_MIN_GAP_SECONDS", "0.03"))
ALIGN_MAX_GAP_SECONDS = float(os.getenv("ALIGN_MAX_GAP_SECONDS", "0.3"))
ALIGN_LEAD_IN_SECONDS = float(os.getenv("ALIGN_LEAD_IN_SECONDS", "0.0"))
ALIGN_TAIL_PAD_SECONDS = float(os.getenv("ALIGN_TAIL_PAD_SECONDS", "0.3"))
# Bounds for the global video retime factor (dub duration / source duration).
# The factor is normally ~1.0-1.6; the floor is 1.0 so the picture is never
# played faster than the source when the narration happens to be shorter. The
# ceiling only guards against a bad ASR/TTS measurement producing an absurd
# render.
ALIGN_MIN_SLOW_FACTOR = float(os.getenv("ALIGN_MIN_SLOW_FACTOR", "1.0"))
ALIGN_MAX_SLOW_FACTOR = float(os.getenv("ALIGN_MAX_SLOW_FACTOR", "4.0"))

WORK_SR = "48000"


def build_continuous_timeline(
    source_spans: list[tuple[float, float]],
    clip_durations: list[float],
    source_duration: float,
    *,
    min_gap: float = ALIGN_MIN_GAP_SECONDS,
    max_gap: float = ALIGN_MAX_GAP_SECONDS,
    lead_in: float = ALIGN_LEAD_IN_SECONDS,
    tail_pad: float = ALIGN_TAIL_PAD_SECONDS,
    min_slow: float = ALIGN_MIN_SLOW_FACTOR,
    max_slow: float = ALIGN_MAX_SLOW_FACTOR,
) -> tuple[list[tuple[float, float]], float]:
    """Place TTS clips on one continuous dub timeline.

    ``source_spans[i]`` is the natural-clock ``(start, end)`` of segment ``i``;
    ``clip_durations[i]`` is the measured speech length after silence trim.
    Each clip starts after the previous one, separated by the source pause
    clamped to ``[min_gap, max_gap]``. A pause inside a merged Whisper segment
    is invisible here and therefore never turns into dead air.

    Returns ``(placements, base_slow)`` where ``placements[i]`` is
    ``(start, duration)`` on the base timeline and ``base_slow`` is the global
    video retime factor (``dub_total / source_duration``), bounded so a bad
    narration measurement cannot produce an absurd render.
    """
    n = min(len(source_spans), len(clip_durations))
    if n <= 0:
        return [], 1.0

    placements: list[tuple[float, float]] = []
    cursor = max(0.0, float(lead_in))
    prev_source_end = 0.0
    for i in range(n):
        clip = max(0.05, float(clip_durations[i] or 0.0))
        source_start, source_end = source_spans[i]
        if i > 0:
            source_gap = max(0.0, float(source_start) - prev_source_end)
            cursor += min(max(source_gap, min_gap), max_gap)
        placements.append((round(cursor, 3), round(clip, 3)))
        cursor += clip
        prev_source_end = max(prev_source_end, float(source_end))

    total = cursor + max(0.0, float(tail_pad))
    if source_duration <= 0:
        return placements, 1.0
    base_slow = total / source_duration
    base_slow = min(max(base_slow, min_slow), max_slow)
    return placements, round(base_slow, 6)


async def probe_duration(path: str | Path) -> float:
    proc = await asyncio.create_subprocess_exec(
        FFPROBE_BIN,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    data = json.loads(out.decode())
    return float(data.get("format", {}).get("duration") or 0)


async def _run_ffmpeg(cmd: list[str], where: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise TransientError(ALIGN_FAILED, (stderr or b"").decode(errors="replace")[-600:])


async def to_wav(src: str | Path, dst: str | Path) -> None:
    """Decode ``src`` to a 48k mono wav, trimming lead/trail silence.

    TTS engines pad every clip (often 0.3-1s). Keeping that padding would put a
    small silence around every line, which the continuous timeline no longer
    hides. Trimming first makes the measured duration the real speech length.
    Falls back to a verbatim decode when trimming fails or leaves an empty file.
    """
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if TRIM_SILENCE:
        try:
            await _trim_leading_trailing_silence(src, dst)
            if await probe_duration(dst) > 0.01:
                return
        except (TransientError, OSError, ValueError, KeyError):
            pass
    tmp = Path(str(dst) + ".tmp.wav")
    await _run_ffmpeg(
        [
            FFMPEG_BIN,
            "-y",
            "-i",
            str(src),
            "-ar",
            WORK_SR,
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(tmp),
        ],
        "wav",
    )
    tmp.replace(dst)


async def speech_map(audio_path: str | Path) -> list[list[float]]:
    """Non-silent [start, end] intervals of the source audio, via ``silencedetect``.

    Used to refine each segment's onset within a BOUNDED window — see
    ``refine_onsets``. It must never collapse many segments to one interval head.
    """
    proc = await asyncio.create_subprocess_exec(
        FFMPEG_BIN,
        "-v",
        "info",
        "-i",
        str(audio_path),
        "-af",
        f"silencedetect=noise={SILENCE_THRESHOLD_DB}dB:d=0.35",
        "-f",
        "null",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    text = stderr.decode(errors="replace")
    starts = [float(m) for m in re.findall(r"silence_start: ([0-9.]+)", text)]
    ends = [float(m) for m in re.findall(r"silence_end: ([0-9.]+)", text)]

    intervals: list[list[float]] = []
    prev = 0.0
    for s, e in zip(starts, ends, strict=False):
        if s > prev:
            intervals.append([prev, s])
        prev = max(prev, e)
    total = await probe_duration(audio_path)
    if prev < total - 0.05:
        intervals.append([prev, total])
    return intervals


def refine_onsets(
    raw_starts: list[float], speech: list[list[float]], window: float = 0.8
) -> list[float]:
    """Refine each segment's onset against the real audio, bounded to ``window``.

    Gemini's start is used as-is unless it clearly lands beside real speech: if
    it falls inside a speech interval and that interval begins within ``window``,
    snap to its start; if it falls in a silent gap, snap to the next speech onset
    when that is within ``window``. Never moves a start by more than ``window``,
    so continuous audio can never collapse segments onto a single time (the
    failure a global VAD snap caused earlier).
    """
    out: list[float] = []
    for st in raw_starts:
        refined = st
        for a, b in speech:
            if a <= st <= b:
                if st - a <= window:
                    refined = a
                break
            if a > st and a - st <= window:
                refined = a
                break
        out.append(refined)
    return out


def _silence_trim_filter() -> str:
    # Strip lead and trail silence (both ends). `silenceremove` trims the start;
    # reverse, trim, reverse again handles the end.
    dbs = f"{SILENCE_THRESHOLD_DB}dB"
    keep = f"{SILENCE_GUARD_SECONDS:.3f}"
    trim_once = f"silenceremove=start_periods=1:start_threshold={dbs}:start_silence={keep}"
    return f"{trim_once},areverse,{trim_once},areverse"


def _tempo_filter(tempo: float) -> str:
    """Time-stretch with pitch preserved. rubberband beats atempo for big ratios."""
    return f"rubberband=tempo={tempo:.4f}"


def _tempo_fallback_filter(tempo: float) -> str:
    # atempo supports 0.5-2.0 per stage; our bounded tempo always fits.
    return f"atempo={tempo:.4f}"


async def _trim_leading_trailing_silence(src: Path, dst: Path) -> None:
    """Write a 48k mono wav of ``src`` with lead/trail silence removed."""
    tmp = Path(str(dst) + ".tmp.wav")
    cmd = [
        FFMPEG_BIN,
        "-y",
        "-i",
        str(src),
        "-af",
        _silence_trim_filter(),
        "-ar",
        WORK_SR,
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(tmp),
    ]
    await _run_ffmpeg(cmd, "silence trim")
    tmp.replace(dst)


async def fit_to_slot(
    src: str | Path,
    dst: str | Path,
    slot_seconds: float,
    min_speed: float | None = None,
    max_speed: float | None = None,
    stretch: bool = True,
) -> dict:
    """Prepare ``src`` mp3 as a 48k mono wav at ``dst``.

    ``stretch=True`` (old behaviour): time-stretch with rubberband/atempo so the
    clip fits ``slot_seconds`` exactly. ``stretch=False``: keep the narration at
    its natural speed — lead/trail silence is trimmed, but the tempo is never
    changed, so the placed clip is exactly as spoken.

    Returns ``{tts_duration, trimmed_duration, slot, ratio, speed, fits, output_duration}``
    for logging/inspection.
    """
    src, dst = Path(src), Path(dst)
    if not src.exists():
        raise PermanentError(ALIGN_FAILED, f"tts clip not found: {src}")
    slot = float(slot_seconds)
    if slot <= 0:
        raise PermanentError(ALIGN_FAILED, f"invalid slot {slot}")

    min_speed = ALIGN_MIN_TEMPO if min_speed is None else float(min_speed)
    max_speed = ALIGN_MAX_TEMPO if max_speed is None else float(max_speed)

    tts_dur = await probe_duration(src)
    if tts_dur <= 0:
        raise TransientError(ALIGN_FAILED, f"could not probe {src}")

    work_input: Path
    trimmed_dur = tts_dur
    work_dir = dst.parent
    work_dir.mkdir(parents=True, exist_ok=True)
    if TRIM_SILENCE:
        trimmed = work_dir / f"{dst.stem}.trimmed.wav"
        await _trim_leading_trailing_silence(src, trimmed)
        trimmed_dur = await probe_duration(trimmed)
        if trimmed_dur <= 0:
            trimmed_dur = tts_dur  # trim broke something — fall back to raw clip
        work_input = trimmed
    else:
        work_input = src

    # Natural pace: no tempo change at all. The video was already slowed so the
    # narration fits — the caller only places this clip at its (refined) start.
    if not stretch:
        tmp = Path(str(dst) + ".tmp.wav")
        cmd = [
            FFMPEG_BIN,
            "-y",
            "-i",
            str(work_input),
            "-ar",
            WORK_SR,
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(tmp),
        ]
        await _run_ffmpeg(cmd, "natural-pace wav")
        tmp.replace(dst)
        out_dur = await probe_duration(dst)
        return {
            "tts_duration": round(tts_dur, 3),
            "trimmed_duration": round(trimmed_dur, 3),
            "slot": round(slot, 3),
            "ratio": round(trimmed_dur / slot, 3),
            "speed": 1.0,
            "fits": True,
            "output_duration": round(out_dur, 3),
        }

    ratio = trimmed_dur / slot

    # Decide the stretch tempo and the target length.
    if min_speed <= ratio <= max_speed:
        tempo = ratio
        fits = True
        target_dur = slot
    elif ratio > max_speed:
        # Too long even at max tempo: hard-trim to the slot so nothing spills.
        tempo = max_speed
        fits = False
        target_dur = slot
    else:  # ratio < min_speed
        # Too short: slow down to min tempo, pad up to the (capped) slot so a
        # one-liner never becomes a stretch of silence.
        tempo = min_speed
        fits = False
        target_dur = min(slot, trimmed_dur / min_speed + SLOW_PAD_BUFFER_SECONDS)

    # rubberband may not sample-exactly hit `target_dur`; apad+atrim force it.
    final_filters = f"{_tempo_filter(tempo)},apad,atrim=duration={target_dur:.3f}"

    tmp = Path(str(dst) + ".tmp.wav")
    cmd = [
        FFMPEG_BIN,
        "-y",
        "-i",
        str(work_input),
        "-filter:a",
        final_filters,
        "-ar",
        WORK_SR,
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(tmp),
    ]
    try:
        await _run_ffmpeg(cmd, "rubberband fit")
    except TransientError:
        # Some builds ship ffmpeg without the rubberband filter — atempo keeps
        # the same guarantee (tempo is bounded to the 0.5-2.0 atempo range).
        fallback = f"{_tempo_fallback_filter(tempo)},apad,atrim=duration={target_dur:.3f}"
        cmd = [
            FFMPEG_BIN,
            "-y",
            "-i",
            str(work_input),
            "-filter:a",
            fallback,
            "-ar",
            WORK_SR,
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(tmp),
        ]
        await _run_ffmpeg(cmd, "atempo fit")

    tmp.replace(dst)
    out_dur = await probe_duration(dst)
    return {
        "tts_duration": round(tts_dur, 3),
        "trimmed_duration": round(trimmed_dur, 3),
        "slot": round(slot, 3),
        "ratio": round(ratio, 3),
        "speed": round(tempo, 3),
        "fits": fits,
        "output_duration": round(out_dur, 3),
    }
