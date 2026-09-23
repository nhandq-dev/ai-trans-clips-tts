"""Fit TTS clips to their time slots (plan/009 T2.2).

Each segment has a slot ``end - start``. TTS may be longer/shorter. We adapt with:
- 0.8 ≤ ratio ≤ 1.3 → atempo=ratio (pitch preserved)
- ratio > 1.3 → atempo=1.3, remainder spills into next gap or is trimmed
- ratio < 0.8 → atempo=0.8 then apad the rest

Output is 48kHz wav for later mux.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from errors import ALIGN_FAILED, PermanentError, TransientError

FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "ffprobe")


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


def _atempo_filter(ratio: float) -> str:
    # atempo only supports 0.5-2.0, and for ratio outside we chain
    # For 0.8-1.3 we are inside, so single atempo
    return f"atempo={ratio:.4f}"


async def fit_to_slot(
    src: str | Path,
    dst: str | Path,
    slot_seconds: float,
    min_speed: float = 0.8,
    max_speed: float = 1.3,
) -> dict:
    """Fit ``src`` mp3 to ``slot_seconds``. Writes wav 48k to ``dst``.

    Returns {tts_duration, slot, ratio, speed, fits} for logging.
    """
    src, dst = Path(src), Path(dst)
    if not src.exists():
        raise PermanentError(ALIGN_FAILED, f"tts clip not found: {src}")
    tts_dur = await probe_duration(src)
    if tts_dur <= 0:
        raise TransientError(ALIGN_FAILED, f"could not probe {src}")
    slot = float(slot_seconds)
    if slot <= 0:
        raise PermanentError(ALIGN_FAILED, f"invalid slot {slot}")
    ratio = tts_dur / slot

    # decide speed and whether it fits
    if min_speed <= ratio <= max_speed:
        speed = ratio
        fits = True
        filter_a = _atempo_filter(speed)
        target_dur = slot
    elif ratio > max_speed:
        # too long: speed up to max, remainder will spill (handled by dub builder)
        speed = max_speed
        fits = False
        filter_a = _atempo_filter(speed)
        target_dur = tts_dur / max_speed  # still > slot, will be trimmed or spill
    else:  # ratio < min_speed
        speed = min_speed
        fits = False
        # slow down to min_speed, then pad to fill slot
        # apad will pad with silence to exactly slot
        filter_a = f"{_atempo_filter(speed)},apad,atrim=duration={slot:.3f}"
        target_dur = slot

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(dst) + ".tmp.wav")
    # Build ffmpeg command
    # For the simple cases, we just atempo; for slow case, filter already includes apad
    if ratio < min_speed:
        filt = filter_a
    elif ratio > max_speed:
        # just atempo, let dub builder handle spill/trim
        filt = filter_a
    else:
        filt = filter_a

    # Normalize to 48k, mono, wav
    cmd = [
        FFMPEG_BIN,
        "-y",
        "-i",
        str(src),
        "-filter:a",
        filt,
        "-ar",
        "48000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(tmp),
    ]
    # For apad case, we need to ensure duration is exactly slot: already in filter
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise TransientError(ALIGN_FAILED, (stderr or b"").decode(errors="replace")[-600:])
    tmp.replace(dst)
    # verify output duration
    out_dur = await probe_duration(dst)
    return {
        "tts_duration": round(tts_dur, 3),
        "slot": round(slot, 3),
        "ratio": round(ratio, 3),
        "speed": round(speed, 3) if "speed" in locals() else None,
        "fits": fits,
        "output_duration": round(out_dur, 3),
    }
