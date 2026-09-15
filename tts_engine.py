from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Callable, Optional

ProgressCallback = Optional[Callable[[int, str], None]]

VIENEU_DEFAULT_VOICE = os.getenv("VIENEU_DEFAULT_VOICE", "Adam")
VIENEU_VOICES = [
    "Minh Đức", "Phạm Tuyên", "Thái Sơn", "Xuân Vĩnh",
    "Thanh Bình", "Trúc Ly", "Ngọc Linh", "Đoan Trang",
    "Mai Anh", "Thục Đoan", "Minh Triết", "Thùy Dung",
    "Quang Sơn", "Ngọc Trân", "Mỹ Duyên", "Quỳnh Anh",
    "Đức Trí", "Kim Thanh", "Ngọc Huyền", "Adam",
]

EDGE_FALLBACK_VOICE = os.getenv("EDGE_FALLBACK_VOICE", "en-US-JennyNeural")
EDGE_DEFAULT_VOICES = {
    "vi": "vi-VN-HoaiMyNeural",
    "en": "en-US-JennyNeural",
    "ja": "ja-JP-NanamiNeural",
    "ko": "ko-KR-SunHiNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
    "fr": "fr-FR-DeniseNeural",
    "es": "es-ES-ElviraNeural",
    "de": "de-DE-KatjaNeural",
    "pt": "pt-BR-FranciscaNeural",
    "it": "it-IT-ElsaNeural",
    "ru": "ru-RU-SvetlanaNeural",
    "th": "th-TH-PremwadeeNeural",
    "id": "id-ID-GadisNeural",
    "hi": "hi-IN-SwaraNeural",
    "ar": "ar-EG-SalmaNeural",
}

DEFAULT_FORMAT = os.getenv("TTS_FORMAT", "mp3").lower().lstrip(".")

_MODEL = None
_MODEL_LOCK = threading.Lock()
_INFER_LOCK = threading.Lock()


def lang_code(language: str) -> str:
    return (language or "").strip().lower().replace("_", "-").split("-")[0]


def is_vietnamese(language: str) -> bool:
    return lang_code(language) == "vi"


def engine_for(language: str) -> str:
    return "vieneu" if is_vietnamese(language) else "edge-tts"


def _get_model():
    global _MODEL
    if _MODEL is None:
        with _MODEL_LOCK:
            if _MODEL is None:
                from vieneu import Vieneu

                _MODEL = Vieneu(backend=os.getenv("VIENEU_BACKEND", "onnx"))
    return _MODEL


def _resolve_vieneu_voice(voice: Optional[str]) -> str:
    value = (voice or "").strip()
    return value if value in VIENEU_VOICES else VIENEU_DEFAULT_VOICE


def _resolve_edge_voice(language: str, voice: Optional[str]) -> str:
    value = (voice or "").strip()
    if value:
        return value
    return EDGE_DEFAULT_VOICES.get(lang_code(language), EDGE_FALLBACK_VOICE)


def _split_text(text: str, max_chars: int) -> list[str]:
    sents = re.split(r"(?<=[.!?。！？\n])\s*", text)
    sents = [s.strip() for s in sents if s.strip()]
    chunks: list[str] = []
    cur = ""
    for s in sents:
        if len(cur) + len(s) + 1 > max_chars and cur:
            chunks.append(cur)
            cur = s
        else:
            cur = (cur + " " + s) if cur else s
    if cur:
        chunks.append(cur)
    return chunks or [text]


def _ffmpeg() -> str:
    return os.getenv("FFMPEG_BIN") or shutil.which("ffmpeg") or "ffmpeg"


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace")[:500])


def _transcode(src: Path, dest: Path) -> None:
    _run([_ffmpeg(), "-y", "-i", str(src), "-b:a", "128k", str(dest)])


def _concat(chunks: list[Path], dest: Path, workdir: Path) -> None:
    list_file = workdir / f"concat_{uuid.uuid4().hex}.txt"
    list_file.write_text("\n".join(f"file '{c.resolve()}'" for c in chunks), encoding="utf-8")
    try:
        _run([
            _ffmpeg(), "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file), "-b:a", "128k", str(dest),
        ])
    finally:
        list_file.unlink(missing_ok=True)


def _merge(chunks: list[Path], dest: Path, src_ext: str, fmt: str, workdir: Path) -> None:
    if len(chunks) == 1:
        src = chunks[0]
        if src_ext == fmt:
            shutil.move(str(src), str(dest))
        else:
            _transcode(src, dest)
            src.unlink(missing_ok=True)
        return
    _concat(chunks, dest, workdir)
    for chunk in chunks:
        chunk.unlink(missing_ok=True)


def _synth_vieneu(
    text: str,
    voice: Optional[str],
    dest: Path,
    fmt: str,
    workdir: Path,
    progress: ProgressCallback,
) -> None:
    model = _get_model()
    resolved = _resolve_vieneu_voice(voice)
    chunks = _split_text(text, int(os.getenv("VIENEU_CHUNK_CHARS", "280")))
    files: list[Path] = []
    for i, chunk in enumerate(chunks):
        if progress:
            progress(10 + int((i / len(chunks)) * 80), f"vieneu {i + 1}/{len(chunks)}")
        wav = workdir / f"vieneu_{i}.wav"
        with _INFER_LOCK:
            audio = model.infer(text=chunk, voice=resolved)
            model.save(audio, str(wav))
        if not wav.exists() or wav.stat().st_size == 0:
            raise RuntimeError(f"vieneu produced no audio for chunk {i + 1}/{len(chunks)}")
        files.append(wav)
    _merge(files, dest, "wav", fmt, workdir)


async def _edge_chunk(text: str, voice: str, dest: Path) -> None:
    from edge_tts import Communicate

    await Communicate(text, voice).save(str(dest))


def _synth_edge(
    text: str,
    language: str,
    voice: Optional[str],
    dest: Path,
    fmt: str,
    workdir: Path,
    progress: ProgressCallback,
) -> None:
    resolved = _resolve_edge_voice(language, voice)
    chunks = _split_text(text, int(os.getenv("EDGE_CHUNK_CHARS", "300")))
    files: list[Path] = []
    for i, chunk in enumerate(chunks):
        if progress:
            progress(10 + int((i / len(chunks)) * 80), f"edge-tts {i + 1}/{len(chunks)}")
        mp3 = workdir / f"edge_{i}.mp3"
        try:
            asyncio.run(_edge_chunk(chunk, resolved, mp3))
        except Exception as exc:
            if "403" in str(exc):
                raise RuntimeError(
                    "Microsoft Edge-TTS blocked (403). Use a proxy or switch to Azure Speech."
                ) from exc
            raise
        if not mp3.exists() or mp3.stat().st_size == 0:
            raise RuntimeError(f"edge-tts produced no audio for chunk {i + 1}/{len(chunks)}")
        files.append(mp3)
    _merge(files, dest, "mp3", fmt, workdir)


def generate_tts(
    text: str,
    language: str = "vi",
    voice: Optional[str] = None,
    out_path: Optional[str | Path] = None,
    fmt: Optional[str] = None,
    progress: ProgressCallback = None,
) -> Path:
    text = (text or "").strip()
    if not text:
        raise ValueError("text is empty")

    fmt = (fmt or DEFAULT_FORMAT).lower().lstrip(".")
    if fmt not in ("mp3", "wav"):
        raise ValueError("format must be 'mp3' or 'wav'")

    if out_path:
        dest = Path(out_path)
    else:
        out_dir = Path(os.getenv("TTS_OUTPUT_DIR", "tts_output"))
        dest = out_dir / f"tts_{uuid.uuid4().hex}.{fmt}"
    dest.parent.mkdir(parents=True, exist_ok=True)

    workdir = dest.parent / f".tmp_{uuid.uuid4().hex}"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        if is_vietnamese(language):
            _synth_vieneu(text, voice, dest, fmt, workdir, progress)
        else:
            _synth_edge(text, language, voice, dest, fmt, workdir, progress)
        if progress:
            progress(100, "Completed")
        return dest
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
