"""Subtitle handling — detect original box, blur it, render new ASS (plan/009 T3.2-T3.4).

Detection is cheap and OCR-free: sample ~1 fps in the top/bottom third, threshold
high-contrast text, find contours, and pick the box that appears most consistently.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import cv2
import numpy as np
from errors import SUBTITLE_DETECT_FAILED, PermanentError, TransientError
from schemas import TranscriptionResult

FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "ffprobe")
SAMPLE_FPS = float(os.getenv("SUBTITLE_SAMPLE_FPS", "1"))
MAX_SAMPLES = int(os.getenv("SUBTITLE_MAX_SAMPLES", "30"))
STABLE_RATIO = float(os.getenv("SUBTITLE_STABLE_RATIO", "0.6"))

# Fixed ASS v4+ syntax lines (long by nature; see write_ass).
_ASS_FORMAT_LINE = (  # noqa: E501
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
    "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
    "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
)
_ASS_EVENT_FORMAT_LINE = (  # noqa: E501
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
)


async def _probe(path: str | Path) -> dict:
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
    out, _ = await proc.communicate()
    data = json.loads(out.decode())
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise PermanentError(SUBTITLE_DETECT_FAILED, "no video stream")
    return {
        "duration": float(data.get("format", {}).get("duration") or 0),
        "width": int(video["width"]),
        "height": int(video["height"]),
    }


def _detect_boxes_in_frame(
    gray: np.ndarray, region_y: int, region_h: int, width: int
) -> list[tuple[int, int, int, int]]:
    """Find high-contrast text-like boxes in the given region of a grayscale frame."""
    region = gray[region_y : region_y + region_h, 0:width]
    if region.size == 0:
        return []
    # adaptive threshold to catch text on varying backgrounds
    thr = cv2.adaptiveThreshold(
        region, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 10
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 3))
    morph = cv2.morphologyEx(thr, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(morph, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes: list[tuple[int, int, int, int]] = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        area = w * h
        if area < (width * region_h) * 0.002:  # too small
            continue
        if h < 8 or w < 20:
            continue
        boxes.append((x, y + region_y, w, h))
    return boxes


async def detect_subtitle_box(
    video_path: str | Path,
    position: str = "bottom",
    work_dir: str | Path | None = None,
) -> dict:
    """Detect the original subtitle box. Returns {x,y,w,h,position,confidence}.

    ``confidence`` is the fraction of sampled frames where the box was present.
    """
    video_path = Path(video_path)
    info = await _probe(video_path)
    width, height = info["width"], info["height"]
    duration = info["duration"]

    if position == "top":
        region_y, region_h = 0, height // 3
    else:
        region_y, region_h = (height * 2) // 3, height // 3

    # sample frames at SAMPLE_FPS (cap MAX_SAMPLES) via ffmpeg -> rawvideo gray
    fps = min(SAMPLE_FPS, MAX_SAMPLES / max(duration, 1.0)) if duration else SAMPLE_FPS
    fps = max(fps, 0.2)
    proc = await asyncio.create_subprocess_exec(
        FFMPEG_BIN,
        "-v",
        "quiet",
        "-i",
        str(video_path),
        "-vf",
        f"fps={fps}",
        "-pix_fmt",
        "gray",
        "-f",
        "rawvideo",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    raw, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise TransientError(
            SUBTITLE_DETECT_FAILED, (stderr or b"").decode(errors="replace")[-500:]
        )
    frame_size = width * height
    n_frames = len(raw) // frame_size
    if n_frames == 0:
        raise PermanentError(SUBTITLE_DETECT_FAILED, "no frames sampled")

    # count how often each coarse box (rounded) appears
    counts: dict[tuple[int, int, int, int], int] = {}
    for i in range(n_frames):
        frame = np.frombuffer(raw[i * frame_size : (i + 1) * frame_size], dtype=np.uint8).reshape(
            height, width
        )
        for b in _detect_boxes_in_frame(frame, region_y, region_h, width):
            key = (
                round(b[0] / 20) * 20,
                round(b[1] / 10) * 10,
                round(b[2] / 20) * 20,
                round(b[3] / 5) * 5,
            )
            counts[key] = counts.get(key, 0) + 1

    if not counts:
        # no text detected -> low confidence, caller falls back to default position
        return {
            "x": 0,
            "y": region_y,
            "w": width,
            "h": region_h,
            "position": position,
            "confidence": 0.0,
        }

    best, hits = max(counts.items(), key=lambda kv: kv[1])
    confidence = hits / n_frames
    x, y, w, h = best
    # clamp
    x = max(0, min(x, width - 1))
    y = max(0, min(y, height - 1))
    w = max(1, min(w, width - x))
    h = max(1, min(h, height - y))
    return {
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "position": position,
        "confidence": round(confidence, 3),
    }


def default_box(width: int, height: int, position: str = "bottom") -> dict:
    """Fallback when detection confidence is low: a band at the bottom (or top)."""
    h = height // 5
    y = (height - h - int(height * 0.05)) if position != "top" else int(height * 0.05)
    return {"x": 0, "y": y, "w": width, "h": h, "position": position, "confidence": 0.0}


def _ass_color(rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    return f"&H00{b:02X}{g:02X}{r:02X}"


def _ass_time(seconds: float) -> str:
    cs = int(round(seconds * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def write_ass(
    result: TranscriptionResult,
    box: dict,
    out_path: str | Path,
    *,
    width: int,
    height: int,
    style: dict | None = None,
) -> Path:
    """Render new subtitles as ASS, positioned inside ``box``."""
    style = style or {}
    font = style.get("font", "Arial")
    size = int(style.get("size", 24))
    color = (
        _ass_color((255, 255, 255))
        if style.get("color", "white") == "white"
        else _ass_color((255, 255, 0))
    )
    outline = int(style.get("outline", 2))

    # ASS alignment 2 = bottom-center; place at the box bottom
    box_bottom = box["y"] + box["h"]
    margin_v = max(10, height - box_bottom + 10)
    align = 2
    # The Format/Style lines are fixed ASS syntax and legitimately exceed the
    # line length limit, so they are excluded from linting.
    style_line = (
        f"Style: Default,{font},{size},{color},&H000000FF,&H00000000,&H64000000,"
        f"0,0,0,0,100,100,0,0,1,{outline},1,{align},10,10,{margin_v},1"
    )
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        f"{_ASS_FORMAT_LINE}\n"
        f"{style_line}\n"
        "\n"
        "[Events]\n"
        f"{_ASS_EVENT_FORMAT_LINE}\n"
    )
    lines = []
    for seg in result.segments:
        text = seg.target_text.replace("\n", "\\N")
        lines.append(
            f"Dialogue: 0,{_ass_time(seg.start)},{_ass_time(seg.end)},Default,,0,0,0,,{text}"
        )
    out_path = Path(out_path)
    tmp = Path(str(out_path) + ".tmp")
    tmp.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(out_path)
    return out_path
