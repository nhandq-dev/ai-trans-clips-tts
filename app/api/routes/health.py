from __future__ import annotations

from fastapi import APIRouter

from app.core.config import get_settings

router = APIRouter()


@router.get("/health")
async def health():
    settings = get_settings()
    return {
        "status": "ok",
        "engines": {"vi": "vieneu", "other": "edge-tts"},
        "concurrency": settings.tts_concurrency,
    }
