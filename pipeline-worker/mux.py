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
            FFMPEG_BIN,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r=48000:cl=mono:d={video_duration:.3f}",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "1",
            str(tmp),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise TransientError(MUX_FAILED, (stderr or b"").decode(errors="replace")[-600:])
        tmp.replace(out_path)
        return out_path

    # Input 0 is a silent track as long as the video; each clip is a further
    # input delayed to its start time, then everything is mixed with amix.
    filter_parts = []
    cmd = [
        FFMPEG_BIN,
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r=48000:cl=mono:d={video_duration:.3f}",
    ]
    for wav, _ in aligned_clips:
        cmd.extend(["-i", str(wav)])
    # [1:a]adelay=..[a1]; ...; [0:a][a1][a2]amix=inputs=N:normalize=0
    for idx, (_, start) in enumerate(aligned_clips, start=1):
        delay_ms = int(round(start * 1000))
        # adelay needs delay for each channel: 1 channel -> delay|delay
        filter_parts.append(f"[{idx}:a]adelay={delay_ms}|{delay_ms}[a{idx}]")
    # amix all
    amix_inputs = "".join(f"[a{idx}]" for idx in range(1, len(aligned_clips) + 1))
    n_inputs = len(aligned_clips) + 1
    filter_parts.append(
        f"[0:a]{amix_inputs}amix=inputs={n_inputs}"
        ":normalize=0:duration=longest:dropout_transition=0[dub]"
    )
    filter_complex = ";".join(filter_parts)
    cmd.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            "[dub]",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "1",
            str(tmp),
        ]
    )
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
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
            FFMPEG_BIN,
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(dub_path),
            "-filter_complex",
            "[1:a]loudnorm=I=-16:TP=-1.5:LRA=11[a]",
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(tmp),
        ]
    else:
        # mix orig at volume + dub
        cmd = [
            FFMPEG_BIN,
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(dub_path),
            "-filter_complex",
            f"[0:a]volume={original_volume_db}dB[orig];[orig][1:a]amix=inputs=2:normalize=0:duration=longest:dropout_transition=0[mixed];[mixed]loudnorm=I=-16:TP=-1.5:LRA=11[a]",
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(tmp),
        ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise TransientError(MUX_FAILED, (stderr or b"").decode(errors="replace")[-800:])
    tmp.replace(out_path)
    return out_path


async def render_translated_video(
    video_path: str | Path,
    dub_path: str | Path,
    ass_path: str | Path | None,
    out_path: str | Path,
    *,
    blur_box: dict | None = None,
    original_volume_db: int = -20,
    mute_original: bool = False,
    subtitle_style: dict | None = None,
) -> Path:
    """Single-pass render (plan/009 T3.5): blur old subs + burn new ASS + mix audio.

    Only one video encode. When ``ass_path`` is None no burn happens; when
    ``blur_box`` is None no blur happens.
    """
    video_path, dub_path, out_path = Path(video_path), Path(dub_path), Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(out_path) + ".tmp.mp4")

    parts: list[str] = []
    vlabel = "0:v"

    if blur_box:
        dims = await _video_size(video_path)
        x, y, w, h = blur_box["x"], blur_box["y"], blur_box["w"], blur_box["h"]
        if dims:
            vw, vh = dims
            x = max(0, min(int(x), vw - 2))
            y = max(0, min(int(y), vh - 2))
            w = max(2, min(int(w), vw - x))
            h = max(2, min(int(h), vh - y))
            # a too-short band makes boxblur impossible (radius must be < min(w,h)/2)
            # and would blur nothing anyway -> fall back to a default bottom band
            if h < 8 or w < 8:
                h = max(8, vh // 5)
                y = max(0, vh - h - max(2, vh // 20))
                w = vw
                x = 0
        # boxblur radius is bounded by min(w, h)/2; keep it valid for small crops
        radius = max(1, min(10, min(w, h) // 2 - 1))
        chroma = max(1, min(5, radius // 2)) if radius >= 2 else 1
        parts.append(
            f"[0:v]crop={w}:{h}:{x}:{y},"
            f"boxblur=luma_radius={radius}:luma_power=2:chroma_radius={chroma}:chroma_power=2[bl];"
            f"[0:v][bl]overlay={x}:{y}[vblur]"
        )
        vlabel = "vblur"

    if ass_path:
        # ass filter needs escaped path; use forward slashes and escape colons
        ass = str(Path(ass_path).resolve()).replace("\\", "/").replace(":", "\\:")
        parts.append(f"[{vlabel}]ass='{ass}'[vass]")
        vlabel = "vass"

    if not parts:
        # no video filters: copy video
        video_map = "0:v"
        vcodec = ["-c:v", "copy"]
    else:
        video_map = f"[{vlabel}]"
        vcodec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]

    has_original_audio = await _has_audio(video_path)
    if mute_original or not has_original_audio:
        parts.append("[1:a]loudnorm=I=-16:TP=-1.5:LRA=11[aout]")
    else:
        parts.append(
            f"[0:a]volume={original_volume_db}dB[orig];"
            f"[orig][1:a]amix=inputs=2:normalize=0:duration=longest:dropout_transition=0[mixed];"
            f"[mixed]loudnorm=I=-16:TP=-1.5:LRA=11[aout]"
        )

    cmd = [FFMPEG_BIN, "-y", "-i", str(video_path), "-i", str(dub_path)]
    cmd += ["-filter_complex", ";".join(parts)]
    cmd += ["-map", video_map, "-map", "[aout]"]
    cmd += vcodec + ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(tmp)]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise TransientError(MUX_FAILED, (stderr or b"").decode(errors="replace")[-900:])
    tmp.replace(out_path)
    return out_path


async def _has_audio(path: str | Path) -> bool:
    import json

    proc = await asyncio.create_subprocess_exec(
        FFPROBE_BIN,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    try:
        data = json.loads(out.decode())
    except Exception:
        return False
    return any(s.get("codec_type") == "audio" for s in data.get("streams", []))


async def _video_size(path: str | Path) -> tuple[int, int] | None:
    import json

    proc = await asyncio.create_subprocess_exec(
        FFPROBE_BIN,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    try:
        data = json.loads(out.decode())
    except Exception:
        return None
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        return None
    return int(v["width"]), int(v["height"])
