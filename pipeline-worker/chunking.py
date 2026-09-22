"""Audio chunking for long videos (plan/009 T1.3).

Splits a FLAC into ~8-10 minute chunks so Gemini can handle 25-minute videos.
Each chunk is transcribed in parallel (2-3 concurrent), then segments are
re-based with the chunk offset so the timeline is continuous.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from gemini import transcribe_and_translate
from schemas import Segment, TranscriptionResult

CHUNK_DURATION_SECONDS = int(os.getenv("CHUNK_DURATION_SECONDS", "510"))  # ~8.5 min
MAX_PARALLEL = int(os.getenv("GEMINI_PARALLEL", "3"))
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")


async def chunk_audio(
    src: str | Path,
    dst_dir: str | Path,
    chunk_duration: int = CHUNK_DURATION_SECONDS,
) -> list[Path]:
    """Split FLAC into chunks of ``chunk_duration`` seconds. Returns sorted chunk paths."""
    src, dst_dir = Path(src), Path(dst_dir)
    if not src.exists():
        raise FileNotFoundError(src)
    dst_dir.mkdir(parents=True, exist_ok=True)
    # clean previous chunks
    for p in dst_dir.glob("chunk_*.flac"):
        p.unlink(missing_ok=True)
        p.with_suffix(p.suffix + ".tmp").unlink(missing_ok=True)
    pattern = str(dst_dir / "chunk_%03d.flac")
    proc = await asyncio.create_subprocess_exec(
        FFMPEG_BIN,
        "-y",
        "-i",
        str(src),
        "-c:a",
        "flac",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-f",
        "segment",
        "-segment_time",
        str(chunk_duration),
        "-reset_timestamps",
        "1",
        pattern,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg chunk failed: {(stderr or b'').decode(errors='replace')[-600:]}"
        )
    chunks = sorted(dst_dir.glob("chunk_*.flac"))
    return chunks


async def _probe_duration(path: Path) -> float:
    import json

    proc = await asyncio.create_subprocess_exec(
        os.getenv("FFPROBE_BIN", "ffprobe"),
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    data = json.loads(stdout.decode())
    return float(data.get("format", {}).get("duration") or 0)


async def transcribe_chunked(
    audio_path: str | Path,
    source_language: str,
    target_language: str,
    work_dir: str | Path | None = None,
) -> TranscriptionResult:
    """Chunk, transcribe each chunk in parallel, merge with offset.

    If audio is <= CHUNK_DURATION, just calls transcribe_and_translate once (no chunking).
    """
    audio_path = Path(audio_path)
    # quick probe duration
    duration = await _probe_duration(audio_path)
    if duration <= CHUNK_DURATION_SECONDS:
        return await transcribe_and_translate(audio_path, source_language, target_language)

    # need chunking
    if work_dir is None:
        work_dir = audio_path.parent / "chunks"
    else:
        work_dir = Path(work_dir)
    chunks = await chunk_audio(audio_path, work_dir)

    # transcribe in parallel with semaphore
    sem = asyncio.Semaphore(MAX_PARALLEL)

    async def _one(chunk: Path, offset: float) -> TranscriptionResult:
        async with sem:
            res = await transcribe_and_translate(chunk, source_language, target_language)
            for seg in res.segments:
                seg.start += offset
                seg.end += offset
            return res

    # compute offsets as index * CHUNK_DURATION_SECONDS (segment muxer splits exactly there)
    offsets: list[float] = [i * CHUNK_DURATION_SECONDS for i in range(len(chunks))]

    tasks = [_one(chunk, off) for chunk, off in zip(chunks, offsets, strict=True)]
    results: list[TranscriptionResult] = await asyncio.gather(*tasks)

    # merge segments, keep first non-unknown detected_language
    all_segments: list[Segment] = []
    detected_language = source_language if source_language != "auto" else "unknown"
    for res in results:
        all_segments.extend(res.segments)
        if detected_language == "unknown" and res.detected_language not in ("unknown", ""):
            detected_language = res.detected_language
    # if still unknown and we have segments, take first result's detected
    if detected_language == "unknown" and results and results[0].detected_language != "unknown":
        detected_language = results[0].detected_language

    return TranscriptionResult(
        detected_language=detected_language, segments=sorted(all_segments, key=lambda s: s.start)
    )
