"""Asynchronous TTS endpoints (plan/014 Phase 1).

The synchronous `POST /v1/tts` is kept for short texts (< `SYNC_MAX_TEXT_LENGTH`),
where waiting a few seconds is fine. Anything longer goes through the queue so the
caller never hits the HTTP timeout.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from app.core.config import get_settings
from app.schemas.tts import TTSRequest, TTSValidationError, validate_tts_request
from app.services import custom_voices
from app.services.job_store import (
    JOB_COMPLETED,
    JOB_FAILED,
    get_job_store,
    new_job,
)

logger = logging.getLogger("tts-worker.jobs")

router = APIRouter()

SSE_POLL_SECONDS = 1.0
SSE_MAX_SECONDS = 3600.0
MEDIA_TYPES = {"mp3": "audio/mpeg", "wav": "audio/wav"}


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


async def _load_owned(job_id: str, user_id: int | None):
    """Fetch a job, treating another user's job as missing.

    The API always passes its authenticated user id, so a job id alone is never
    enough to read someone else's audio. Callers that omit it (internal tooling
    signing with the same key) get the job back.
    """
    job = await get_job_store().get(job_id)
    if job is None:
        return None
    if user_id is not None and job.user_id is not None and job.user_id != user_id:
        logger.warning("job_owner_mismatch job=%s", job_id)
        return None
    return job


@router.post("/v1/tts/jobs", status_code=202)
async def create_job(req: TTSRequest) -> dict:
    """Enqueue a synthesis and return immediately with the job id."""
    settings = get_settings()
    store = get_job_store()
    if settings.tts_workers > 1 and not store.shared:
        raise HTTPException(
            status_code=503,
            detail="async TTS requires REDIS_URL when TTS_WORKERS is greater than 1",
        )

    # Shared Free-tier pool: the worker has only a few lanes, so without a ceiling a
    # rush of free users would queue hours behind each other (plan/015).
    if req.tier == "free":
        active = await store.count_active("free")
        if active >= settings.tts_free_active_job_limit:
            logger.warning(
                "free_queue_full active=%d limit=%d", active, settings.tts_free_active_job_limit
            )
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "FREE_QUEUE_FULL",
                    "message": (
                        "The Free queue is full right now. Please try again in a few minutes."
                    ),
                },
            )

    voice_data = None
    if req.voice and req.owner is not None:
        voice_data = await asyncio.to_thread(custom_voices.load_voice, req.owner, req.voice)

    try:
        fmt = validate_tts_request(req, custom_voice=voice_data is not None, async_job=True)
    except TTSValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job = new_job(
        text=req.text,
        language=req.language,
        voice=req.voice,
        fmt=fmt,
        owner=req.owner,
        user_id=req.user_id,
        priority=req.priority,
        tier=req.tier,
    )
    await store.create(job)
    if req.tier:
        await store.add_active(req.tier, job.id)
    await store.enqueue(job.id, req.priority)
    logger.info(
        "job_queued job=%s chars=%d language=%s priority=%d",
        job.id,
        job.text_length,
        job.language,
        job.priority,
    )
    return job.public()


@router.get("/v1/tts/jobs/{job_id}")
async def get_job(job_id: str, user_id: int | None = None) -> dict:
    job = await _load_owned(job_id, user_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job.public()


@router.get("/v1/tts/jobs/{job_id}/events")
async def job_events(
    job_id: str, request: Request, user_id: int | None = None
) -> StreamingResponse:
    """Server-sent events with the job's state, ending on a terminal status."""

    async def stream() -> AsyncIterator[str]:
        last: dict | None = None
        elapsed = 0.0
        while True:
            if await request.is_disconnected():
                break
            job = await _load_owned(job_id, user_id)
            if job is None:
                yield _sse({"job_id": job_id, "status": "unknown", "error": "job not found"})
                break
            payload = job.public()
            if payload != last:
                last = payload
                yield _sse(payload)
            if job.status in (JOB_COMPLETED, JOB_FAILED):
                break
            if elapsed >= SSE_MAX_SECONDS:
                yield _sse({**payload, "error": "stream timed out"})
                break
            await asyncio.sleep(SSE_POLL_SECONDS)
            elapsed += SSE_POLL_SECONDS

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/v1/tts/jobs/{job_id}/audio")
async def job_audio(job_id: str, user_id: int | None = None) -> FileResponse:
    job = await _load_owned(job_id, user_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status == JOB_FAILED:
        raise HTTPException(status_code=409, detail=job.error or "job failed")
    if job.status != JOB_COMPLETED or not job.audio_path:
        raise HTTPException(status_code=409, detail=f"job is {job.status}")

    path = Path(job.audio_path)
    if not path.exists() or path.stat().st_size == 0:
        raise HTTPException(status_code=410, detail="audio is no longer available")

    return FileResponse(
        path,
        media_type=MEDIA_TYPES.get(job.fmt, "application/octet-stream"),
        filename=f"speech.{job.fmt}",
    )
