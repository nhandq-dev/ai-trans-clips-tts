from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from app.core.config import get_settings
from app.services.chunking import split_text
from app.services.engine_router import EDGE_DEFAULT_VOICES, is_vietnamese, lang_code
from app.services.storage import cleanup_workdir, make_workdir, merge, output_dir
from app.services.voice_catalog import VIENEU as VIENEU_ENGINE
from app.services.voice_catalog import VOICES

ProgressCallback = Callable[[int, str], None] | None

# Valid VieNeu voice ids, derived from the catalog to avoid a second source of truth.
VIENEU_VOICES = [voice["id"] for voice in VOICES if voice["engine"] == VIENEU_ENGINE]

_MODEL = None
_MODEL_LOCK = threading.Lock()
_INFER_LOCK = threading.Lock()


def _get_model():
    global _MODEL
    if _MODEL is None:
        with _MODEL_LOCK:
            if _MODEL is None:
                from vieneu import Vieneu

                _MODEL = Vieneu(backend=get_settings().vieneu_backend)
    return _MODEL


def warm_up() -> None:
    """Load the VieNeu model ahead of the first request.

    Enabled via `READINESS_WARMUP`; edge-tts is remote and needs no warmup.
    """
    _get_model()


def _resolve_vieneu_voice(voice: str | None) -> str:
    value = (voice or "").strip()
    return value if value in VIENEU_VOICES else get_settings().vieneu_default_voice


def _resolve_edge_voice(language: str, voice: str | None) -> str:
    value = (voice or "").strip()
    if value:
        return value
    return EDGE_DEFAULT_VOICES.get(lang_code(language), get_settings().edge_fallback_voice)


def _synth_vieneu(
    text: str,
    voice: str | None,
    dest: Path,
    fmt: str,
    workdir: Path,
    progress: ProgressCallback,
) -> None:
    model = _get_model()
    resolved = _resolve_vieneu_voice(voice)
    chunks = split_text(text, get_settings().vieneu_chunk_chars)
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
    merge(files, dest, "wav", fmt, workdir)


async def _edge_chunk(text: str, voice: str, dest: Path) -> None:
    from edge_tts import Communicate

    await Communicate(text, voice).save(str(dest))


def _synth_edge(
    text: str,
    language: str,
    voice: str | None,
    dest: Path,
    fmt: str,
    workdir: Path,
    progress: ProgressCallback,
) -> None:
    resolved = _resolve_edge_voice(language, voice)
    chunks = split_text(text, get_settings().edge_chunk_chars)
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
    merge(files, dest, "mp3", fmt, workdir)


def generate_tts(
    text: str,
    language: str = "vi",
    voice: str | None = None,
    out_path: str | Path | None = None,
    fmt: str | None = None,
    progress: ProgressCallback = None,
) -> Path:
    text = (text or "").strip()
    if not text:
        raise ValueError("text is empty")

    fmt = (fmt or get_settings().tts_format).lower().lstrip(".")
    if fmt not in ("mp3", "wav"):
        raise ValueError("format must be 'mp3' or 'wav'")

    if out_path:
        dest = Path(out_path)
    else:
        dest = output_dir() / f"tts_{uuid.uuid4().hex}.{fmt}"
    dest.parent.mkdir(parents=True, exist_ok=True)

    workdir = make_workdir(dest.parent)
    try:
        if is_vietnamese(language):
            _synth_vieneu(text, voice, dest, fmt, workdir, progress)
        else:
            _synth_edge(text, language, voice, dest, fmt, workdir, progress)
        if progress:
            progress(100, "Completed")
        return dest
    finally:
        cleanup_workdir(workdir)
