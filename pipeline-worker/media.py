"""Media helpers — probe + extract audio for Gemini (plan/009 T1.1)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from errors import NO_AUDIO_TRACK, PermanentError, TransientError

FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "ffprobe")


async def probe(path: str | Path) -> dict:
    """Return {duration, width, height, has_audio, audio_codec}. Raises on failure."""
    path = Path(path)
    if not path.exists():
        raise PermanentError("NOT_FOUND", f"file not found: {path}")
    proc = await asyncio.create_subprocess_exec(
        FFPROBE_BIN,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise TransientError("PROBE_FAILED", (stderr or b"").decode(errors="replace")[:500])
    data = json.loads(stdout.decode())
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    duration = float(fmt.get("duration") or 0)
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return {
        "duration": duration,
        "width": int(video["width"]) if video and "width" in video else None,
        "height": int(video["height"]) if video and "height" in video else None,
        "has_audio": audio is not None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "streams": streams,
    }


async def extract_audio(src: str | Path, dst: str | Path) -> Path:
    """Extract mono 16k FLAC for Gemini. Raises NO_AUDIO_TRACK if no audio stream."""
    src, dst = Path(src), Path(dst)
    info = await probe(src)
    if not info["has_audio"]:
        raise PermanentError(NO_AUDIO_TRACK, "no audio track")
    dst.parent.mkdir(parents=True, exist_ok=True)
    # atomic: write to str(dst)+".tmp" and force the flac muxer so the .tmp
    # suffix does not make ffmpeg guess the wrong output format
    tmp = Path(str(dst) + ".tmp")
    proc = await asyncio.create_subprocess_exec(
        FFMPEG_BIN,
        "-y",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "flac",
        "-f",
        "flac",
        str(tmp),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        # No audio is already handled above; this is a real ffmpeg failure
        raise TransientError("EXTRACT_FAILED", (stderr or b"").decode(errors="replace")[-800:])
    tmp.replace(dst)
    return dst


def extract_audio_sync(src: str | Path, dst: str | Path) -> Path:
    import asyncio as _asyncio

    return _asyncio.run(extract_audio(src, dst))


async def extract_thumbnail(
    video_path: str | Path,
    dst: str | Path,
    at_seconds: float | None = None,
    width: int = 640,
) -> Path:
    """Grab one frame as a JPEG thumbnail (used by the projects grid).

    Defaults to ~10% into the video (capped at 3s) so we skip intros/black
    frames while staying early enough to be representative.
    """
    video_path, dst = Path(video_path), Path(dst)
    if at_seconds is None:
        info = await probe(video_path)
        duration = info.get("duration") or 0
        at_seconds = min(3.0, duration * 0.1) if duration > 1 else 0.0
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(dst) + ".tmp")
    proc = await asyncio.create_subprocess_exec(
        FFMPEG_BIN,
        "-y",
        "-ss",
        f"{at_seconds:.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:-2",
        "-q:v",
        "4",
        "-f",
        "image2",
        "-c:v",
        "mjpeg",
        str(tmp),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise TransientError("THUMBNAIL_FAILED", (stderr or b"").decode(errors="replace")[-400:])
    tmp.replace(dst)
    return dst
