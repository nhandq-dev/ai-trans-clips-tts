"""Whisper ASR — timeline + source text for dubbing (plan: whisper-asr pipeline).

Replaces Gemini for the "listen & time" half of transcription. Whisper (via
faster-whisper, CTranslate2) runs on CPU (int8), transcribes the natural-speed
audio, and returns segments in strict chronological order with accurate
timestamps. Translation is a separate concern handled by gemini.translate_text.

The model downloads to ``WHISPER_DOWNLOAD_ROOT`` on first use (image stays slim)
and is cached, so only the first container start pays the download cost.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path

from errors import PermanentError, TransientError
from schemas import Segment, TranscriptionResult

logger = logging.getLogger("pipeline-worker.asr")

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "medium")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_BEAM_SIZE = int(os.getenv("WHISPER_BEAM_SIZE", "5"))
WHISPER_CPU_THREADS = int(os.getenv("WHISPER_CPU_THREADS", "4"))
WHISPER_VAD_FILTER = os.getenv("WHISPER_VAD_FILTER", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)
DOWNLOAD_ROOT = os.getenv("WHISPER_DOWNLOAD_ROOT") or str(
    Path(os.getenv("CACHE_DIR", ".")) / "whisper"
)

# Minimum/split heuristics for turning whisper text into dubbing segments.
MIN_SEGMENT_SECONDS = float(os.getenv("ASR_MIN_SEGMENT_SECONDS", "0.4"))
MAX_SEGMENT_SECONDS = float(os.getenv("ASR_MAX_SEGMENT_SECONDS", "8"))

_model: object | None = None
_model_lock = threading.Lock()


def _load_model() -> object:
    """Lazily build (and cache) the Whisper model. Heavy first call (download)."""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            try:
                from faster_whisper import WhisperModel

                _model = WhisperModel(
                    WHISPER_MODEL,
                    device=WHISPER_DEVICE,
                    compute_type=WHISPER_COMPUTE_TYPE,
                    cpu_threads=WHISPER_CPU_THREADS,
                    download_root=DOWNLOAD_ROOT,
                )
                logger.info(
                    "whisper model ready model=%s device=%s compute=%s threads=%d",
                    WHISPER_MODEL,
                    WHISPER_DEVICE,
                    WHISPER_COMPUTE_TYPE,
                    WHISPER_CPU_THREADS,
                )
            except Exception as exc:
                raise TransientError("ASR_FAILED", f"whisper model load failed: {exc}") from exc
    return _model


def _split_long_segment(text: str, start: float, end: float) -> list[tuple[str, float, float]]:
    """Split an extra-long segment on punctuation so TTS/subtitle slots stay short.

    Whisper segments can be one long run-on line; dubbing needs bite-sized slots.
    When punctuation splitting produces nothing, the whole segment is kept.
    """
    duration = end - start
    if duration <= MAX_SEGMENT_SECONDS:
        return [(text, start, end)]
    pieces = [p for p in (_p.strip() for _p in _split_on_punct(text)) if p]
    if not pieces:
        return [(text, start, end)]
    # Proportion each piece's duration to its character share; the last piece
    # absorbs the rounding remainder so the split always covers the segment.
    total_chars = sum(len(p) for p in pieces)
    parts: list[tuple[str, float, float]] = []
    cursor = start
    for idx, piece in enumerate(pieces):
        if idx == len(pieces) - 1:
            parts.append((piece, cursor, end))
        else:
            piece_dur = duration * len(piece) / total_chars
            parts.append((piece, cursor, cursor + piece_dur))
            cursor += piece_dur
    if len(parts) == 1:
        return [(text, start, end)]
    return parts


def _split_on_punct(text: str) -> list[str]:
    import re

    # Split after sentence-ending punctuation (. ! ? 。！？...) keeping the mark.
    return re.split(r"(?<=[.!?。！？])\s+", text)


async def transcribe_whisper(
    audio_path: str | Path,
    source_language: str = "auto",
) -> TranscriptionResult:
    """Transcribe + timestamp ``audio_path`` (16k mono FLAC) with Whisper.

    Returns segments with ``target_text`` left empty (filled by translation).
    Timestamps are on the NATURAL clock (no slowdown applied).
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise PermanentError("ASR_FAILED", f"audio not found: {audio_path}")

    model = await _await_model()

    def _do() -> tuple[str, list[Segment]]:
        seg_gen, info = model.transcribe(  # type: ignore[union-attr]
            str(audio_path),
            language=None if source_language == "auto" else source_language,
            beam_size=WHISPER_BEAM_SIZE,
            vad_filter=WHISPER_VAD_FILTER,
            word_timestamps=False,
        )
        segments: list[Segment] = []
        for s in seg_gen:
            text = (s.text or "").strip()
            if not text:
                continue
            for piece, pstart, pend in _split_long_segment(text, float(s.start), float(s.end)):
                if pend - pstart < MIN_SEGMENT_SECONDS:
                    continue
                segments.append(
                    Segment(
                        start=round(pstart, 3),
                        end=round(pend, 3),
                        source_text=piece,
                        target_text="",
                    )
                )
        return (info.language or "unknown"), segments

    try:
        lang, segments = await asyncio.to_thread(_do)
    except Exception as exc:
        raise TransientError("ASR_FAILED", f"whisper transcribe failed: {exc}") from exc

    # Diagnostic: record raw Whisper output for comparison against the final markdown.
    try:
        raw_path = Path(str(audio_path) + ".whisper.json")
        raw_path.write_text(
            json.dumps(
                {
                    "model": WHISPER_MODEL,
                    "lang": lang,
                    "segments": [
                        {
                            "start": round(s.start, 3),
                            "end": round(s.end, 3),
                            "text": s.source_text,
                        }
                        for s in segments
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.info(
            "WHISPER_RAW file=%s lang=%s n=%d",
            audio_path.name,
            lang,
            len(segments),
        )
    except Exception as exc:  # diagnostic must never fail a job
        logger.warning("WHISPER_RAW write failed: %s", exc)

    return TranscriptionResult(detected_language=lang, segments=segments)


async def _await_model() -> object:
    """Build the model in a worker thread so the event loop never blocks."""
    return await asyncio.to_thread(_load_model)
