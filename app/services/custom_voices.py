from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from app.core.config import get_settings
from app.services import object_storage, synthesis
from app.services.storage import ffmpeg_bin, run
from app.services.voice_catalog import greeting_for

logger = logging.getLogger("tts-worker.custom_voices")

_VOICE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_CACHED_EMBEDDINGS = 128


class CustomVoiceError(ValueError):
    """A clone request the caller can fix (mapped to HTTP 400)."""


class CustomVoiceNotFound(LookupError):
    """The requested custom voice does not exist for this owner (mapped to HTTP 404)."""


@dataclass(frozen=True)
class CloneResult:
    voice_id: str
    name: str
    language: str
    engine: str
    sample_key: str
    duration_ms: int


_EMBEDDINGS: dict[str, object] = {}
_CACHE_LOCK = Lock()


def _prefix(owner: int, voice_id: str | None = None) -> str:
    base = f"{get_settings().custom_voice_prefix}/{owner}"
    return f"{base}/{voice_id}" if voice_id else base


def _meta_key(owner: int, voice_id: str) -> str:
    return f"{_prefix(owner, voice_id)}/meta.json"


def _reference_key(owner: int, voice_id: str) -> str:
    return f"{_prefix(owner, voice_id)}/reference.wav"


def _sample_key(owner: int, voice_id: str) -> str:
    return f"{_prefix(owner, voice_id)}/sample.mp3"


def is_voice_id(value: str | None) -> bool:
    return bool(value) and bool(_VOICE_ID_RE.match(value or ""))


def _cache_get(voice_id: str) -> object | None:
    with _CACHE_LOCK:
        return _EMBEDDINGS.get(voice_id)


def _cache_put(voice_id: str, value: object) -> None:
    with _CACHE_LOCK:
        if len(_EMBEDDINGS) >= _MAX_CACHED_EMBEDDINGS:
            _EMBEDDINGS.pop(next(iter(_EMBEDDINGS)))
        _EMBEDDINGS[voice_id] = value


def _cache_evict(voice_id: str) -> None:
    with _CACHE_LOCK:
        _EMBEDDINGS.pop(voice_id, None)


def _probe_duration(path: Path) -> float:
    settings = get_settings()
    probe = settings.ffprobe_bin or "ffprobe"
    proc = subprocess.run(
        [
            probe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise CustomVoiceError("could not read the audio file") from None
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:
        raise CustomVoiceError("could not read the audio duration") from exc


def clone_voice(
    *,
    owner: int,
    name: str,
    language: str,
    filename: str,
    data: bytes,
    request_id: str = "",
) -> CloneResult:
    settings = get_settings()
    if not settings.custom_voice_enabled:
        raise CustomVoiceError("custom voices are disabled")

    language = (language or "").strip().lower()
    if language != "vi":
        raise CustomVoiceError("custom voices are only available for Vietnamese (vi)")

    clean_name = (name or "").strip()[:40]
    if not clean_name:
        raise CustomVoiceError("name is required")
    if not data:
        raise CustomVoiceError("audio file is required")
    if not object_storage.is_configured():
        raise CustomVoiceError("custom voices are not configured")

    voice_id = uuid.uuid4().hex
    workdir = Path(tempfile.mkdtemp(prefix="clone_"))
    try:
        source = workdir / f"source{Path(filename or 'reference.wav').suffix or '.bin'}"
        source.write_bytes(data)

        duration = _probe_duration(source)
        tolerance = 0.25
        if (
            duration < settings.custom_voice_min_seconds - tolerance
            or duration > settings.custom_voice_max_seconds + tolerance
        ):
            raise CustomVoiceError(
                "reference audio must be "
                f"{settings.custom_voice_min_seconds:g}"
                f"-{settings.custom_voice_max_seconds:g} seconds"
            )

        reference_wav = workdir / "reference.wav"
        run([ffmpeg_bin(), "-y", "-i", str(source), "-ac", "1", "-ar", "24000", str(reference_wav)])
        voice_data = synthesis.encode_reference(reference_wav)

        sample_wav = workdir / "sample.wav"
        synthesis.render_sample(greeting_for("vi"), voice_data, sample_wav)
        sample_mp3 = workdir / "sample.mp3"
        run([ffmpeg_bin(), "-y", "-i", str(sample_wav), "-b:a", "128k", str(sample_mp3)])

        meta = {
            "voice_id": voice_id,
            "owner": owner,
            "name": clean_name,
            "language": "vi",
            "engine": "vieneu",
            "reference_key": _reference_key(owner, voice_id),
            "sample_key": _sample_key(owner, voice_id),
            "duration_ms": int(round(duration * 1000)),
            "created_at": int(time.time()),
        }
        object_storage.put_bytes(
            _reference_key(owner, voice_id), reference_wav.read_bytes(), "audio/wav"
        )
        object_storage.put_bytes(
            _sample_key(owner, voice_id), sample_mp3.read_bytes(), "audio/mpeg"
        )
        object_storage.put_bytes(
            _meta_key(owner, voice_id), json.dumps(meta).encode("utf-8"), "application/json"
        )
        _cache_put(voice_id, voice_data)
        logger.info(
            "custom_voice_cloned",
            extra={
                "request_id": request_id,
                "voice_id": voice_id,
                "owner": owner,
                "duration_ms": meta["duration_ms"],
            },
        )
        return CloneResult(
            voice_id=voice_id,
            name=clean_name,
            language="vi",
            engine="vieneu",
            sample_key=meta["sample_key"],
            duration_ms=meta["duration_ms"],
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def load_voice(owner: int, voice_id: str) -> object | None:
    """Return the cached/encoded VieNeu voice for the owner, or None if unknown."""
    if not is_voice_id(voice_id):
        return None

    cached = _cache_get(voice_id)
    if cached is not None:
        return cached
    if not object_storage.is_configured():
        return None

    try:
        meta = json.loads(object_storage.get_bytes(_meta_key(owner, voice_id)))
    except Exception:
        return None
    if not isinstance(meta, dict) or meta.get("owner") != owner:
        return None

    try:
        reference = object_storage.get_bytes(_reference_key(owner, voice_id))
    except Exception:
        return None

    workdir = Path(tempfile.mkdtemp(prefix="load_"))
    try:
        reference_wav = workdir / "reference.wav"
        reference_wav.write_bytes(reference)
        voice_data = synthesis.encode_reference(reference_wav)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    _cache_put(voice_id, voice_data)
    return voice_data


def get_sample(owner: int, voice_id: str) -> bytes:
    if not is_voice_id(voice_id):
        raise CustomVoiceNotFound("custom voice not found")
    try:
        return object_storage.get_bytes(_sample_key(owner, voice_id))
    except Exception as exc:
        raise CustomVoiceNotFound("custom voice not found") from exc


def delete_voice(owner: int, voice_id: str) -> None:
    if not is_voice_id(voice_id):
        raise CustomVoiceNotFound("custom voice not found")
    try:
        object_storage.get_bytes(_meta_key(owner, voice_id))
    except Exception as exc:
        raise CustomVoiceNotFound("custom voice not found") from exc
    object_storage.delete_prefix(_prefix(owner, voice_id))
    _cache_evict(voice_id)
