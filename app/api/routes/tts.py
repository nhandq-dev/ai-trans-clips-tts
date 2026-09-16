from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.core.config import get_settings
from app.schemas.tts import TTSRequest, TTSResponse
from app.services.engine_router import engine_for
from app.services.synthesis import generate_tts

router = APIRouter()

_semaphore = asyncio.Semaphore(get_settings().tts_concurrency)


@router.post("/tts", response_model=None)
async def tts(request: Request, req: TTSRequest):
    fmt = req.format.lower().lstrip(".")
    if fmt not in ("mp3", "wav"):
        raise HTTPException(status_code=400, detail="format must be 'mp3' or 'wav'")

    request.state.engine = engine_for(req.language)
    request.state.language = req.language
    request.state.text_length = len(req.text)

    output_dir = get_settings().tts_output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    dest = output_dir / f"tts_{uuid.uuid4().hex}.{fmt}"

    async with _semaphore:
        try:
            path = await asyncio.to_thread(
                generate_tts,
                req.text,
                req.language,
                req.voice,
                str(dest),
                fmt,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"TTS failed: {exc}") from exc

    media_type = "audio/mpeg" if fmt == "mp3" else "audio/wav"
    return FileResponse(
        path=str(path),
        media_type=media_type,
        filename=path.name,
        background=BackgroundTask(lambda p=path: Path(p).unlink(missing_ok=True)),
    )


@router.post("/tts/meta", response_model=TTSResponse)
async def tts_meta(request: Request, req: TTSRequest):
    request.state.engine = engine_for(req.language)
    request.state.language = req.language
    request.state.text_length = len(req.text)

    output_dir = get_settings().tts_output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    dest = output_dir / f"tts_{uuid.uuid4().hex}.{req.format.lower().lstrip('.')}"

    async with _semaphore:
        try:
            path = await asyncio.to_thread(
                generate_tts,
                req.text,
                req.language,
                req.voice,
                str(dest),
                req.format,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"TTS failed: {exc}") from exc

    return TTSResponse(
        engine=engine_for(req.language),
        language=req.language,
        voice=req.voice or "",
        output_path=str(path),
    )
