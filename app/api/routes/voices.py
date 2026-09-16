from __future__ import annotations

from fastapi import APIRouter

from app.services.voice_catalog import catalog

router = APIRouter()


@router.get("/v1/voices")
async def voices():
    return catalog()
