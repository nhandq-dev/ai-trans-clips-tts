"""Kokoro-82M TTS on ONNX Runtime (`kokoro-onnx`).

Replaces edge-tts for every non-Vietnamese language: everything runs on-device,
so there is no network dependency and no Microsoft 403 blocking at synthesis
time. Fits the existing CPU/ONNX stack (VieNeu already uses onnxruntime).

Model files (`kokoro-v1.0.fp16.onnx` ~164 MB + `voices-v1.0.bin` ~28 MB) are
downloaded once from the official release into the model dir and cached on disk;
override the URLs via `KOKORO_MODEL_URL`/`KOKORO_VOICES_URL` for air-gapped
deploys. The fp16 export runs on CPU and halves working RAM vs fp32 with
negligible quality loss.
"""

from __future__ import annotations

import fcntl
import shutil
import threading
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf

from app.core.config import get_settings
from app.services.engine_router import kokoro_lang_for

# fp16 export of kokoro-v1.0: ~164 MB on disk and ~half the working RAM of the
# full-precision model while staying essentially lossless (spectral correlation
# ~0.999 per the upstream release notes). Override the URL for the fp32/int8
# variants if a deploy needs them.
MODEL_FILE = "kokoro-v1.0.fp16.onnx"
VOICES_FILE = "voices-v1.0.bin"
RELEASE_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1"

_MODEL = None
_MODEL_LOCK = threading.Lock()
_INFER_LOCK = threading.Lock()


def model_dir() -> Path:
    settings = get_settings()
    if settings.kokoro_model_dir:
        return settings.kokoro_model_dir
    base = settings.hf_home or Path(".kokoro")
    return base / "kokoro"


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    with urllib.request.urlopen(url) as resp, tmp.open("wb") as fh:
        shutil.copyfileobj(resp, fh)
    tmp.replace(dest)


def _ensure_file(local: Path, url: str, default_url: str) -> Path:
    """Return `local`, downloading it if missing.

    Wrapped in a cross-process flock: with `TTS_WORKERS > 1` every uvicorn worker
    boots through the same warmup, and two of them must never write the same
    `.tmp` file at once. The lock only serializes the download; workers that find
    the file already present skip it entirely.
    """
    if local.exists():
        return local
    lock = local.with_name(local.name + ".lock")
    with lock.open("wb"):
        # A fresh file descriptor per creation gets us the open lock.
        pass
    with lock.open("wb") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            if not local.exists():
                _download(url or default_url, local)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
    lock.unlink(missing_ok=True)
    return local


def ensure_model() -> tuple[Path, Path]:
    """Return (model_path, voices_path), downloading the files when missing.

    Both files are language-agnostic: `kokoro-v1.0.onnx` is the multilingual
    Kokoro-82M v1.0 and `voices-v1.0.bin` holds all 54 preset voices.
    """
    settings = get_settings()
    d = model_dir()
    model = _ensure_file(d / MODEL_FILE, settings.kokoro_model_url, f"{RELEASE_URL}/{MODEL_FILE}")
    voices = _ensure_file(
        d / VOICES_FILE, settings.kokoro_voices_url, f"{RELEASE_URL}/{VOICES_FILE}"
    )
    return model, voices


def get_model():
    global _MODEL
    if _MODEL is None:
        with _MODEL_LOCK:
            if _MODEL is None:
                from kokoro_onnx import Kokoro

                model_path, voices_path = ensure_model()
                _MODEL = Kokoro(str(model_path), str(voices_path))
    return _MODEL


def synth_to_wav(language: str, text: str, voice: str, dest: Path) -> None:
    """Synthesize `text` in the given language and write a 24 kHz WAV to `dest`."""
    model = get_model()
    lang = kokoro_lang_for(language)
    # The model and the espeak-ng G2P are process-global and not thread-safe;
    # serialize inference exactly like VieNeu (_INFER_LOCK pattern).
    with _INFER_LOCK:
        samples, sample_rate = model.create(
            text, voice=voice, speed=get_settings().kokoro_speed, lang=lang
        )
    audio = np.asarray(samples, dtype=np.float32)
    if audio.size == 0:
        raise RuntimeError(f"kokoro produced no audio for {lang}/{voice}")
    sf.write(str(dest), audio, sample_rate)
