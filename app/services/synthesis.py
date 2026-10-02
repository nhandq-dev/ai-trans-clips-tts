from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from pathlib import Path

import numpy as np

from app.core.config import get_settings
from app.services.chunking import split_text
from app.services.engine_router import default_voice_for, is_vietnamese
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
    """Load the VieNeu and Kokoro models ahead of the first request.

    Enabled via `READINESS_WARMUP`; both engines are local now, so preloading
    pays off. Set `KOKORO_WARMUP=false` to keep Kokoro lazy on a tight-RAM VPS.
    """
    _get_model()
    if get_settings().kokoro_warmup:
        from app.services import kokoro

        kokoro.get_model()


def _resolve_vieneu_voice(voice: str | None) -> str:
    value = (voice or "").strip()
    return value if value in VIENEU_VOICES else get_settings().vieneu_default_voice


def to_vieneu_voice_arg(voice_data: object | None) -> dict | None:
    """Wrap an encoded reference as the preset dict VieNeu's ``infer`` expects.

    ``encode_reference`` returns ``(speaker_emb, ref_codes)``, but ``infer(voice=...)``
    only understands a preset *name* or a preset *dict*. A bare tuple matches neither,
    so VieNeu silently falls back to ``self._default_voice`` — which is why a cloned
    voice came out sounding like the built-in default (Adam).
    """
    if voice_data is None:
        return None
    speaker_emb, ref_codes = voice_data  # type: ignore[misc]
    return {
        "speaker_emb": np.asarray(speaker_emb, dtype=np.float32),
        "codes": None if ref_codes is None else np.asarray(ref_codes, dtype=np.int64),
    }


def _synth_vieneu(
    text: str,
    voice: str | None,
    dest: Path,
    fmt: str,
    workdir: Path,
    progress: ProgressCallback,
    voice_data: object | None = None,
) -> None:
    model = _get_model()
    resolved = (
        to_vieneu_voice_arg(voice_data) if voice_data is not None else _resolve_vieneu_voice(voice)
    )
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


def _resolve_kokoro_voice(language: str, voice: str | None) -> str:
    value = (voice or "").strip()
    if value:
        return value
    return default_voice_for(language)


def _synth_kokoro(
    text: str,
    language: str,
    voice: str | None,
    dest: Path,
    fmt: str,
    workdir: Path,
    progress: ProgressCallback,
) -> None:
    from app.services import kokoro as kokoro_svc

    resolved = _resolve_kokoro_voice(language, voice)
    chunks = split_text(text, get_settings().kokoro_chunk_chars)
    files: list[Path] = []
    for i, chunk in enumerate(chunks):
        if progress:
            progress(10 + int((i / len(chunks)) * 80), f"kokoro {i + 1}/{len(chunks)}")
        wav = workdir / f"kokoro_{i}.wav"
        kokoro_svc.synth_to_wav(language, chunk, resolved, wav)
        if not wav.exists() or wav.stat().st_size == 0:
            raise RuntimeError(f"kokoro produced no audio for chunk {i + 1}/{len(chunks)}")
        files.append(wav)
    merge(files, dest, "wav", fmt, workdir)


def generate_tts(
    text: str,
    language: str = "vi",
    voice: str | None = None,
    out_path: str | Path | None = None,
    fmt: str | None = None,
    progress: ProgressCallback = None,
    voice_data: object | None = None,
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
            _synth_vieneu(text, voice, dest, fmt, workdir, progress, voice_data)
        else:
            _synth_kokoro(text, language, voice, dest, fmt, workdir, progress)
        if progress:
            progress(100, "Completed")
        return dest
    finally:
        cleanup_workdir(workdir)


def encode_reference(reference_wav: Path) -> object:
    """Encode a reference clip into a reusable VieNeu voice embedding."""
    with _INFER_LOCK:
        return _get_model().encode_reference(str(reference_wav))


def render_sample(text: str, voice_data: object, dest: Path) -> None:
    """Synthesize `text` with a cloned voice and save the raw wav to `dest`."""
    model = _get_model()
    with _INFER_LOCK:
        audio = model.infer(text=text, voice=to_vieneu_voice_arg(voice_data))
        model.save(audio, str(dest))
