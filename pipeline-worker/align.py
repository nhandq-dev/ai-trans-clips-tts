"""Fit TTS clips to their time slots (plan/009 T2.2).

Each segment has a slot ``end - start``. TTS output is usually longer (a slower
target voice) or occasionally shorter than the slot, so the clip is
time-stretched with ffmpeg's ``rubberband`` filter and then padded/trimmed until
it is *exactly* the slot length. Because every clip is clamped to its own slot,
the dub track can never overlap the following segment:

- 0.5 ≤ ratio ≤ 2.0 → ``rubberband=tempo=ratio`` (exact fit, pitch preserved)
- ratio > 2.0       → ``rubberband=tempo=2.0``, excess is hard-trimmed (no overlap)
- ratio < 0.5       → ``rubberband=tempo=0.5``, padded up to the (capped) slot

Lead/trail silence the TTS engines add is stripped first so the measured ratio
reflects real speech rather than padding (a couple hundred ms of silence would
otherwise inflate every ratio and push otherwise-fine segments past the cap).

Output is 48 kHz mono wav for later mux.
"""

from __future__ import annotations

import asyncio
import json
import os
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

WORK_SR = "48000"


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
) -> dict:
    """Fit ``src`` mp3 to ``slot_seconds``. Writes 48k mono wav to ``dst``.

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
