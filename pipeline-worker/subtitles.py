"""Subtitle handling — detect original box, blur it, render new ASS (plan/009 T3.2-T3.4).

Detection prefers OCR (EasyOCR), ported from the legacy video-translator-saas
worker: it samples frames, runs EasyOCR's text detector, clusters detections
into vertical lines and keeps the dominant line — so the real subtitle row is
found accurately and watermarks are filtered out. The cv2 contour heuristic is
kept as a fallback when EasyOCR is not installed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path

import cv2
import numpy as np
from errors import SUBTITLE_DETECT_FAILED, PermanentError, TransientError
from schemas import TranscriptionResult

logger = logging.getLogger("pipeline-worker.subtitles")

FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "ffprobe")
SAMPLE_FPS = float(os.getenv("SUBTITLE_SAMPLE_FPS", "1"))
MAX_SAMPLES = int(os.getenv("SUBTITLE_MAX_SAMPLES", "30"))
STABLE_RATIO = float(os.getenv("SUBTITLE_STABLE_RATIO", "0.6"))
# How much of the frame height is scanned for subtitles (bottom/top half).
# 0.5 scans the whole half so bands sitting above the classic bottom-third strip
# are still found.
REGION_RATIO = float(os.getenv("SUBTITLE_REGION_RATIO", "0.5"))

# OCR scan params (legacy port): how far into the video to sample, the sampling
# step, and how many seconds of confirmation after the first text hit.
OCR_MAX_DURATION = float(os.getenv("SUBTITLE_OCR_MAX_DURATION", "120"))
OCR_INTERVAL = float(os.getenv("SUBTITLE_OCR_INTERVAL", "2"))
OCR_CONFIRM_SECONDS = float(os.getenv("SUBTITLE_OCR_CONFIRM_SECONDS", "2"))

# Upper bound (px) for the detected subtitle band height; shared with mux.py so
# the blur band and the box never disagree. 120 covers large two-line captions.
MAX_BLUR_HEIGHT = int(os.getenv("SUBTITLE_MAX_BLUR_HEIGHT", "120"))

_OCR_READER: object | None = None
_OCR_READER_LOCK = threading.Lock()


def _ocr_reader() -> object:
    """Lazily build (and cache) the EasyOCR reader. Heavy first call (model download)."""
    global _OCR_READER
    if _OCR_READER is not None:
        return _OCR_READER
    with _OCR_READER_LOCK:
        if _OCR_READER is None:
            import easyocr

            _OCR_READER = easyocr.Reader(
                ["en"],
                gpu=False,
                verbose=False,
                model_storage_directory=os.getenv("EASYOCR_MODEL_DIR") or None,
            )
    return _OCR_READER


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
    """Find high-contrast subtitle-line blobs in the given region.

    Subtitle lines are wide and short. After thresholding and a wide horizontal
    closing, keep only wide-and-short blobs and reject full-width solid bars
    (letterbox edges / watermarks) and tiny specks.
    """
    region = gray[region_y : region_y + region_h, 0:width]
    if region.size == 0:
        return []
    # adaptive threshold to catch text on varying backgrounds
    thr = cv2.adaptiveThreshold(
        region, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 10
    )
    # wide kernel merges characters into solid horizontal line blobs
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (35, 5))
    morph = cv2.morphologyEx(thr, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(morph, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_w = max(40, int(width * 0.15))
    max_h = max(12, int(region_h * 0.4))
    boxes: list[tuple[int, int, int, int]] = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if h < 10 or w < min_w:
            continue
        if h > max_h:
            continue
        if w > int(width * 0.98):
            continue
        if w * h < (width * region_h) * 0.004:
            continue
        boxes.append((x, y + region_y, w, h))
    return boxes


async def _detect_box_cv2(
    video_path: str | Path,
    position: str = "bottom",
) -> dict:
    """cv2 contour fallback. Returns {x,y,w,h,position,confidence}."""
    video_path = Path(video_path)
    info = await _probe(video_path)
    width, height = info["width"], info["height"]
    duration = info["duration"]

    if position == "top":
        region_y, region_h = 0, int(height * REGION_RATIO)
    else:
        region_h = max(8, int(height * REGION_RATIO))
        region_y, region_h = height - region_h, region_h

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

    # Vote on the vertical band (rounded y, h) across frames, then take the
    # horizontal extent (min x .. max right edge) wherever that band appeared,
    # so the blur covers the whole row even if the subtitle is centered/varies.
    bands: dict[tuple[int, int], dict[str, float]] = {}
    for i in range(n_frames):
        frame = np.frombuffer(raw[i * frame_size : (i + 1) * frame_size], dtype=np.uint8).reshape(
            height, width
        )
        for b in _detect_boxes_in_frame(frame, region_y, region_h, width):
            key = (round(b[1] / 10) * 10, round(b[3] / 5) * 5)
            acc = bands.setdefault(
                key, {"n": 0, "min_x": float(width), "max_rx": 0.0, "y": 0.0, "h": 0.0}
            )
            acc["n"] += 1
            acc["min_x"] = min(acc["min_x"], float(b[0]))
            acc["max_rx"] = max(acc["max_rx"], float(b[0] + b[2]))
            acc["y"] += b[1]
            acc["h"] += b[3]

    if not bands:
        # no text detected -> low confidence, caller falls back to default position
        return {
            "x": 0,
            "y": region_y,
            "w": width,
            "h": region_h,
            "position": position,
            "confidence": 0.0,
        }

    _, acc = max(bands.items(), key=lambda kv: kv[1]["n"])
    confidence = acc["n"] / n_frames
    y = int(acc["y"] / acc["n"])
    h = max(1, int(acc["h"] / acc["n"]))
    x = max(0, int(acc["min_x"]))
    w = max(1, int(acc["max_rx"]) - x)
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


def _detect_with_easyocr(video_path: Path, position: str) -> dict | None:
    """OCR-based detection, ported from the legacy video-translator-saas worker.

    Samples frames every ``OCR_INTERVAL`` seconds up to ``OCR_MAX_DURATION``,
    runs EasyOCR's text detector on the top/bottom region, clusters the detected
    boxes into vertical lines and keeps the dominant line (the one that appears
    most consistently at the same height — filters watermarks/one-off text).
    Returns None when no subtitle text is found.
    """
    reader = _ocr_reader()
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise TransientError(SUBTITLE_DETECT_FAILED, "cannot open video for OCR")

    all_boxes: list[list[int]] = []
    first_detect_frame: int | None = None
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if width == 0 or height == 0 or fps == 0:
            raise TransientError(SUBTITLE_DETECT_FAILED, "cannot determine video dims for OCR")

        sample_count = min(total_frames, int(fps * OCR_MAX_DURATION))
        step = max(1, int(fps * OCR_INTERVAL))
        region_h = max(8, int(height * REGION_RATIO))

        for frame_idx in range(0, sample_count, step):
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            h, w, _ = frame.shape
            if position == "top":
                crop_y = 0
            else:
                crop_y = h - region_h
            region = frame[crop_y : crop_y + region_h, 0:w]

            detections = reader.detect(region)
            boxes = detections[0] if detections else None
            frame_has_text = False
            if boxes is not None and len(boxes) > 0 and boxes[0] is not None:
                for box in boxes[0]:
                    if box is None or len(box) < 4:
                        continue
                    x_min, x_max, y_min, y_max = box[0], box[1], box[2], box[3]
                    bw = x_max - x_min
                    bh = y_max - y_min
                    # Reject tiny fragments (logos, specks) that are not readable text.
                    if bw < 30 or bh < 10:
                        continue
                    frame_has_text = True
                    all_boxes.append(
                        [int(x_min), int(x_max), int(y_min + crop_y), int(y_max + crop_y)]
                    )

            if frame_has_text and first_detect_frame is None:
                first_detect_frame = frame_idx
            # Adaptive stop: once detected, scan `confirm_seconds` more then stop.
            if first_detect_frame is not None:
                elapsed = (frame_idx - first_detect_frame) / fps
                if elapsed >= OCR_CONFIRM_SECONDS:
                    break
    finally:
        cap.release()

    if not all_boxes:
        return None

    # Cluster detections into vertical lines; the dominant cluster is the subtitle.
    centers = [(b[2] + b[3]) / 2 for b in all_boxes]
    clusters: list[dict] = []
    for c in sorted(centers):
        placed = False
        for cl in clusters:
            if abs(c - cl["mean"]) <= 40:
                cl["vals"].append(c)
                cl["mean"] = sum(cl["vals"]) / len(cl["vals"])
                placed = True
                break
        if not placed:
            clusters.append({"vals": [c], "mean": c})

    best = max(clusters, key=lambda cl: len(cl["vals"]))
    band_min = min(best["vals"])
    band_max = max(best["vals"])
    best_boxes = [b for b in all_boxes if band_min - 10 <= (b[2] + b[3]) / 2 <= band_max + 10]

    arr = np.array(best_boxes)
    final_y_min = int(np.min(arr[:, 2]))

    padding = 10
    # Anchor to the top edge of the dominant line, cap the region height.
    result_y = max(0, final_y_min - padding)
    if width > height:
        result_w = int(width * 0.65)
        result_x = int((width - result_w) / 2)
    else:
        result_w = width - 2 * padding
        result_x = padding
    result_h = min(MAX_BLUR_HEIGHT, max(1, height - result_y))

    logger.info(
        "OCR subtitle region: x=%d y=%d w=%d h=%d (from %d detections in dominant line, %d total)",
        result_x,
        result_y,
        result_w,
        result_h,
        len(best_boxes),
        len(all_boxes),
    )
    return {
        "x": result_x,
        "y": result_y,
        "w": result_w,
        "h": result_h,
        "position": position,
        "confidence": 0.9,
    }


async def detect_subtitle_box(
    video_path: str | Path,
    position: str = "bottom",
    work_dir: str | Path | None = None,
) -> dict:
    """Detect the original subtitle box. Returns {x,y,w,h,position,confidence}.

    Prefers OCR (EasyOCR); falls back to the cv2 contour heuristic when OCR is
    unavailable. ``confidence`` < 0.3 makes the caller skip the blur.
    """
    video_path = Path(video_path)
    try:
        box = await asyncio.to_thread(_detect_with_easyocr, video_path, position)
        if box is not None:
            return box
        logger.warning("OCR found no subtitle line; falling back to cv2 heuristic")
    except Exception as exc:
        logger.warning("OCR detection unavailable (%s); falling back to cv2", exc)
    return await _detect_box_cv2(video_path, position)


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
    # Default font scales with the frame height (e.g. 60px on 1080p); an explicit
    # "size" in subtitle_style still wins.
    size = int(style.get("size", 0)) or max(28, min(64, height // 18))
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
