from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Response

from app.core.config import get_settings
from app.schemas.health import LiveResponse, ReadyResponse
from app.services import storage

router = APIRouter()


def _ffmpeg_available() -> bool:
    return shutil.which(storage.ffmpeg_bin()) is not None


def _output_dir_writable() -> bool:
    directory = get_settings().tts_output_dir
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".ready_{uuid.uuid4().hex}"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def _model_cache_accessible() -> bool:
    hf_home = get_settings().hf_home
    if hf_home:
        directory = Path(hf_home)
    else:
        default = Path.home() / ".cache" / "huggingface"
        directory = Path(os.environ.get("HF_HOME", str(default)))
    try:
        directory.mkdir(parents=True, exist_ok=True)
        return os.access(directory, os.W_OK)
    except OSError:
        return False


def _signing_keys_loaded() -> bool:
    settings = get_settings()
    # Keys are mandatory in production (enforced at startup) and optional in development.
    return bool(settings.hmac_keys) or not settings.is_production


def _concurrency_ready() -> bool:
    return get_settings().tts_concurrency >= 1


@router.get("/health/live", response_model=LiveResponse)
async def live() -> LiveResponse:
    """Liveness: the process is up and the event loop is serving requests."""
    return LiveResponse(status="ok")


@router.get("/health/ready", response_model=ReadyResponse)
async def ready(response: Response) -> ReadyResponse:
    """Readiness: dependencies are usable. Deliberately does not run a synthesis."""
    checks = {
        "ffmpeg": _ffmpeg_available(),
        "output_dir": _output_dir_writable(),
        "model_cache": _model_cache_accessible(),
        "signing_keys": _signing_keys_loaded(),
        "concurrency": _concurrency_ready(),
    }
    reasons = [name for name, ok in checks.items() if not ok]
    if reasons:
        response.status_code = 503
        return ReadyResponse(status="unavailable", checks=checks, reasons=reasons)
    return ReadyResponse(status="ok", checks=checks, reasons=[])
