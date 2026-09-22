"""Mux helpers — build dub track, mix with original, mux with video (plan/009 T2.3).

The dub track is built from aligned wav clips placed at their start times.
We use ``anullsrc`` silence + ``adelay`` per clip + ``amix``. This keeps one
ffmpeg pass for dub+mix+mux when possible (T3.5).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from errors import MUX_FAILED, TransientError

FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "ffprobe")


async def probe_duration(path: str | Path) -> float:
    import json

    proc = await asyncio.create_subprocess_exec(
        FFPROBE_BIN, "-v", "quiet", "-print_format", "json", "-show_format", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    data = json.loads(out.decode())
    return float(data.get("format", {}).get("duration") or 0)


async def build_dub_track(
    aligned_clips: list[tuple[Path, float]],  # (wav_path, start_seconds)
    video_duration: float,
    out_path: str | Path,
) -> Path:
    """Build dub.wav by placing each clip at ``start`` on a silent track."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(out_path) + ".tmp.wav")
    if not aligned_clips:
        # just silence
        proc = await asyncio.create_subprocess_exec(
            FFMPEG_BIN, "-y", "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=mono:d={video_duration:.3f}",
            "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "1", str(tmp),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise TransientError(MUX_FAILED, (stderr or b"").decode(errors="replace")[-600:])
        tmp.replace(out_path)
        return out_path

    # Build filter: each clip -> adelay, then amix
    # Example: [0][1]amix=inputs=2 etc. But we need silence base + N clips.
    # Simpler: use anullsrc as base, then each clip adelay, then amix all.
    inputs = []
    filter_parts = []
    # base silence is input 0 via anullsrc lavfi, but we can also just use -f lavfi as input
    # Instead, we will use ffmpeg with anullsrc + each clip as inputs.
    # Construct command: ffmpeg -f lavfi -i anullsrc=... -i clip0.wav -i clip1.wav ...
    cmd = [FFMPEG_BIN, "-y", "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=mono:d={video_duration:.3f}"]
    for wav, _ in aligned_clips:
        cmd.extend(["-i", str(wav)])
    # filters: [1:a]adelay=0|0[ a1 ]; [2:a]adelay=1234|1234 [a2]; [0:a][a1][a2]amix=inputs=3:normalize=0:duration=longest
    for idx, (_, start) in enumerate(aligned_clips, start=1):
        delay_ms = int(round(start * 1000))
        # adelay needs delay for each channel: 1 channel -> delay|delay
        filter_parts.append(f"[{idx}:a]adelay={delay_ms}|{delay_ms}[a{idx}]")
    # amix all
    amix_inputs = "".join(f"[a{idx}]" for idx in range(1, len(aligned_clips) + 1))
    filter_parts.append(f"[0:a]{amix_inputs}amix=inputs={len(aligned_clips)+1}:normalize=0:duration=longest:dropout_transition=0[dub]")
    filter_complex = ";".join(filter_parts)
    cmd.extend(["-filter_complex", filter_complex, "-map", "[dub]", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "1", str(tmp)])
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise TransientError(MUX_FAILED, (stderr or b"").decode(errors="replace")[-800:])
    tmp.replace(out_path)
    return out_path


async def mix_and_mux(
    video_path: str | Path,
    dub_path: str | Path,
    out_path: str | Path,
    original_volume_db: int = -20,
    mute_original: bool = False,
) -> Path:
    """Mix dub with original audio (at -20dB) and mux with video.

    If ``mute_original`` is True, only dub is used. Otherwise:
        [0:a]volume=-20dB[orig];[orig][1:a]amix=inputs=2:normalize=0:duration=longest[mixed]
    Then loudnorm and mux -c:v copy -c:a aac -b:a 192k -movflags +faststart.
    """
    video_path, dub_path, out_path = Path(video_path), Path(dub_path), Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(out_path) + ".tmp.mp4")
    if mute_original:
        # just replace audio
        cmd = [
            FFMPEG_BIN, "-y",
            "-i", str(video_path),
            "-i", str(dub_path),
            "-filter_complex", "[1:a]loudnorm=I=-16:TP=-1.5:LRA=11[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            str(tmp),
        ]
    else:
        # mix orig at volume + dub
        cmd = [
            FFMPEG_BIN, "-y",
            "-i", str(video_path),
            "-i", str(dub_path),
            "-filter_complex",
            f"[0:a]volume={original_volume_db}dB[orig];[orig][1:a]amix=inputs=2:normalize=0:duration=longest:dropout_transition=0[mixed];[mixed]loudnorm=I=-16:TP=-1.5:LRA=11[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            str(tmp),
        ]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise TransientError(MUX_FAILED, (stderr or b"").decode(errors="replace")[-800:])
    tmp.replace(out_path)
    return out_path
