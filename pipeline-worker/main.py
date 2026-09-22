"""Pipeline worker — download → Gemini → TTS → subtitle → mux.

FastAPI service orchestrating the three VPS workers and Gemini. Exposed as
``POST /translate`` behind HMAC ``X-Pipeline-*`` headers (see auth.py). Mirrors
the conventions of video-worker and tts-worker in this repo (auth middleware,
health endpoint, concurrency semaphore, BackgroundTask cleanup, X-Request-Id).

Stages (plan/009 §4.2): downloading → extracting → transcribing → detecting_subs
→ synthesizing → aligning → muxing → done. Resume is stage-level (§4.5): each
stage writes atomically (.tmp → mv) before marking done in stage_state.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

import jobs as job_store
from auth import HMACAuthMiddleware, RequestContextMiddleware
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from security import parse_keys

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

HMAC_KEYS_JSON = os.getenv("HMAC_KEYS_JSON", "")
CONCURRENCY = int(os.getenv("PIPELINE_CONCURRENCY", "1"))
WORK_DIR = Path(os.getenv("WORK_DIR") or (Path(__file__).parent / "output"))
CACHE_DIR = Path(os.getenv("CACHE_DIR") or (Path(__file__).parent / "cache"))
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.7-flash")
GEMINI_FALLBACK_MODELS = os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.6-flash,gemini-3.5-flash")

CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "600"))
QUEUE_MAX_DEPTH = int(os.getenv("QUEUE_MAX_DEPTH", "100"))

logger = logging.getLogger("pipeline-worker")

_metrics: dict[str, Any] = {
    "requests": 0,
    "success": 0,
    "failed": 0,
    "by_stage": {},  # stage -> count
    "by_code": {},  # error code -> count
    "total_duration": 0.0,
    "started_at": time.time(),
}


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(_cleanup_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            await job_store.cleanup_expired_files(WORK_DIR)
        except Exception as exc:  # never let cleanup kill the loop
            logger.warning("cleanup failed: %s", exc)


app = FastAPI(title="Pipeline Worker", version="0.1.0", lifespan=lifespan)

app.add_middleware(RequestContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",")],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(HMACAuthMiddleware)

_semaphore = asyncio.Semaphore(CONCURRENCY)


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class TranslateOptions(BaseModel):
    original_audio_volume_db: int = Field(default=-20, ge=-60, le=0)
    mute_original: bool = False
    subtitle_position: str = Field(default="bottom", pattern="^(bottom|top)$")
    remove_original_subtitles: bool = True
    burn_subtitles: bool = True
    min_speed: float = Field(default=0.8, ge=0.5, le=1.0)
    max_speed: float = Field(default=1.3, ge=1.0, le=2.0)


class TranslateRequest(BaseModel):
    source_url: str | None = Field(default=None, min_length=8, max_length=2048)
    source_key: str | None = Field(default=None, min_length=1, max_length=1024)
    source_language: str = Field(default="auto", min_length=2, max_length=10)
    target_language: str = Field(default="vi", min_length=2, max_length=10)
    voice: str | None = Field(default=None, max_length=100)
    options: TranslateOptions = Field(default_factory=TranslateOptions)
    job_id: str | None = Field(default=None, max_length=64)


def _canonical_idempotency_key(req: TranslateRequest, user_id: str | None) -> str:
    raw = json.dumps(
        {
            "user_id": user_id or "",
            "source_url": req.source_url or "",
            "source_key": req.source_key or "",
            "source_language": req.source_language,
            "target_language": req.target_language,
            "voice": req.voice or "",
            "options": req.options.model_dump(),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Health / metrics
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "concurrency": CONCURRENCY,
        "work_dir": str(WORK_DIR),
        "signing_configured": bool(parse_keys(HMAC_KEYS_JSON)),
        "gemini_configured": bool(GEMINI_API_KEY),
    }


@app.get("/metrics")
async def metrics():
    uptime = time.time() - _metrics["started_at"]
    avg = _metrics["total_duration"] / _metrics["requests"] if _metrics["requests"] else 0
    return {
        **_metrics,
        "uptime_seconds": round(uptime, 1),
        "avg_duration_seconds": round(avg, 2),
    }


# ---------------------------------------------------------------------------
# Pipeline — stub (T1.x will fill each stage)
# ---------------------------------------------------------------------------


async def _process_job(job_id: str, request: TranslateRequest, request_id: str | None) -> None:
    """Orchestrate stages. Stub for T0.2 — transitions queued → done without real work.

    T1.x will replace this with: download → extract → gemini → TTS → mux.
    Resume (§4.5) checks stage_state/artifacts before each stage.
    """
    start = time.time()
    _metrics["requests"] += 1
    try:
        await job_store.update_job(job_id, status="processing", stage="downloading", progress=5)

        async with _semaphore:
            # TODO T1.1: probe + extract audio
            await job_store.update_job(
                job_id, stage="downloading", progress=10, stage_state={"downloading": "done"}
            )
            await asyncio.sleep(0.05)

            # TODO T1.2: gemini transcribe+translate
            if not GEMINI_API_KEY:
                raise RuntimeError("GEMINI_NOT_CONFIGURED")
            await job_store.update_job(
                job_id,
                stage="transcribing",
                progress=30,
                stage_state={"downloading": "done", "transcribing": "done"},
            )
            await asyncio.sleep(0.05)

            # TODO T2.x: TTS + mux
            await job_store.update_job(job_id, stage="muxing", progress=90)
            await asyncio.sleep(0.05)

        await job_store.update_job(
            job_id,
            status="completed",
            stage="done",
            progress=100,
            stage_state={"downloading": "done", "transcribing": "done", "muxing": "done"},
        )
        _metrics["success"] += 1
    except Exception as exc:
        code = str(exc) if "GEMINI" in str(exc) or "DOWNLOAD" in str(exc) else "FAILED"
        _metrics["failed"] += 1
        _metrics["by_code"][code] = _metrics["by_code"].get(code, 0) + 1
        await job_store.update_job(
            job_id, status="failed", progress=100, error={"code": code, "message": str(exc)}
        )
    finally:
        _metrics["total_duration"] += time.time() - start


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.post("/translate", status_code=202)
async def create_translate_job(req: TranslateRequest, request: Request):
    if not req.source_url and not req.source_key:
        raise HTTPException(
            status_code=400,
            detail={"code": "MISSING_SOURCE", "message": "source_url or source_key is required"},
        )
    if req.source_url and req.source_key:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "CONFLICT",
                "message": "provide either source_url or source_key, not both",
            },
        )

    # Backpressure (T5.7 stub: queue depth check)
    jobs = await job_store.list_jobs(limit=200)
    queued = sum(1 for j in jobs if j.status in ("queued", "processing"))
    if queued >= QUEUE_MAX_DEPTH:
        return JSONResponse(
            status_code=503,
            content={"error": "queue_full", "reason": "too many queued jobs, try again later"},
            headers={"Retry-After": "30"},
        )

    request_id = getattr(request.state, "request_id", None)
    idempotency_key = request.headers.get("Idempotency-Key") or _canonical_idempotency_key(
        req, None
    )

    # Idempotency: return existing job if still active
    existing = await job_store.find_by_idempotency_key(None, idempotency_key)
    if existing:
        return existing.to_dict()

    job = await job_store.create_job(
        source_url=req.source_url,
        source_key=req.source_key,
        source_language=req.source_language,
        target_language=req.target_language,
        voice=req.voice,
        options=req.options.model_dump(),
        idempotency_key=idempotency_key,
        user_id=None,
        request_id=request_id,
        job_id=req.job_id,
    )
    asyncio.create_task(_process_job(job.job_id, req, request_id))
    return JSONResponse(status_code=202, content=job.to_dict())


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = await job_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@app.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    job = await job_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status in ("completed", "failed", "canceled"):
        return job.to_dict()
    await job_store.update_job(
        job_id, status="canceled", error={"code": "CANCELED", "message": "canceled by user"}
    )
    return (await job_store.get_job(job_id)).to_dict()  # type: ignore[union-attr]


@app.get("/jobs/{job_id}/file")
async def get_job_file(job_id: str):
    job = await job_store.get_job(job_id)
    if not job or job.status != "completed" or not job.file_path:
        raise HTTPException(status_code=404, detail="File not ready")
    path = Path(job.file_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")
    return FileResponse(path=str(path), media_type="video/mp4", filename=job.file_name or path.name)


@app.get("/jobs/{job_id}/transcript")
async def get_transcript(job_id: str, format: str = "json"):
    job = await job_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    # TODO T1.4: return transcript.md / .srt / json from artifacts
    if format not in ("json", "md", "srt"):
        raise HTTPException(status_code=400, detail="format must be json, md or srt")
    return {"job_id": job_id, "format": format, "segments": []}


@app.get("/jobs/{job_id}/events")
async def job_events(job_id: str):
    job = await job_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_gen():
        current = await job_store.get_job(job_id)
        if current:
            yield f"data: {json.dumps(current.to_dict())}\n\n"
            if current.status in ("completed", "failed", "canceled"):
                return
        q = job_store.add_listener(job_id)
        try:
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=30)
                    yield f"data: {json.dumps(data)}\n\n"
                    if data.get("status") in ("completed", "failed", "canceled"):
                        break
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            job_store.remove_listener(job_id, q)

    from fastapi.responses import StreamingResponse

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8006")),
    )
