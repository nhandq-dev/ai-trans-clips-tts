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
import shutil
import time
from pathlib import Path
from typing import Any

import jobs as job_store
from auth import HMACAuthMiddleware, RequestContextMiddleware
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from scheduler import get_scheduler
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
    "started_jobs": 0,  # jobs the scheduler actually started
    "total_wait": 0.0,  # summed queue wait (created -> started)
    "started_at": time.time(),
}


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    cleanup_task = asyncio.create_task(_cleanup_loop())
    disk_task = asyncio.create_task(_disk_guard_loop())
    watchdog_task = asyncio.create_task(_watchdog_loop())
    scheduler = get_scheduler(_process_job)
    await scheduler.start()
    logger.info(
        "scheduler started",
        extra={"concurrency": scheduler.snapshot()["concurrency"]},
    )
    try:
        yield
    finally:
        await scheduler.stop()
        for task in (cleanup_task, disk_task, watchdog_task):
            task.cancel()
        for task in (cleanup_task, disk_task, watchdog_task):
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            await job_store.cleanup_expired_files(WORK_DIR)
            await _cleanup_work_dirs()
        except Exception as exc:  # never let cleanup kill the loop
            logger.warning("cleanup failed: %s", exc)


async def _cleanup_work_dirs() -> None:
    """Drop WORK_DIR/{job_id} trees older than WORK_DIR_TTL_SECONDS (T5.2)."""
    ttl = int(os.getenv("WORK_DIR_TTL_SECONDS", "86400"))
    if not WORK_DIR.exists():
        return
    now = time.time()
    for child in WORK_DIR.iterdir():
        if not child.is_dir():
            continue
        try:
            if now - child.stat().st_mtime > ttl:
                await asyncio.to_thread(shutil.rmtree, child, True)
                logger.info("removed expired work dir %s", child.name)
        except Exception:
            continue


async def _watchdog_loop() -> None:
    """Fail jobs that would otherwise sit in `queued`/`processing` forever.

    Silent stalls are the worst failure mode: the UI shows a never-ending spinner
    and the user has no idea what is wrong. This loop converts every stall into an
    explicit, actionable error code.

    * scheduler not alive  -> `SCHEDULER_DOWN` (the worker will never start jobs)
    * queued too long      -> `QUEUE_TIMEOUT`
    * processing too long  -> `JOB_TIMEOUT`
    """
    interval = int(os.getenv("WATCHDOG_INTERVAL_SECONDS", "15"))
    queue_timeout = int(os.getenv("QUEUE_TIMEOUT_SECONDS", "900"))
    job_timeout = int(os.getenv("JOB_TIMEOUT_SECONDS", "1800"))

    while True:
        await asyncio.sleep(interval)
        try:
            await _reap_stalled_jobs(queue_timeout, job_timeout)
        except Exception as exc:  # never let the watchdog die
            logger.warning("watchdog failed: %s", exc)


async def _reap_stalled_jobs(queue_timeout: int, job_timeout: int) -> None:
    now = time.time()
    scheduler_alive = True
    try:
        scheduler_alive = get_scheduler().alive
    except Exception:
        scheduler_alive = False

    for job in await job_store.list_jobs(limit=500):
        if job.status == "queued":
            waited = now - job.created_at
            if not scheduler_alive:
                await _fail_stalled(
                    job.job_id,
                    "SCHEDULER_DOWN",
                    "The worker's job scheduler is not running, so this job was never "
                    "started. This is a server-side fault — please retry.",
                )
            elif waited > queue_timeout:
                await _fail_stalled(
                    job.job_id,
                    "QUEUE_TIMEOUT",
                    f"Job stayed queued for {int(waited)}s (limit {queue_timeout}s) and was "
                    "never started. The worker may be overloaded or stuck.",
                )
        elif job.status == "processing":
            idle = now - job.updated_at
            if idle > job_timeout:
                await _fail_stalled(
                    job.job_id,
                    "JOB_TIMEOUT",
                    f"Job made no progress for {int(idle)}s (limit {job_timeout}s) and was "
                    "aborted.",
                )


async def _fail_stalled(job_id: str, code: str, message: str) -> None:
    logger.error("failing stalled job %s: %s - %s", job_id, code, message)
    await job_store.update_job(
        job_id,
        status="failed",
        progress=100,
        error={"code": code, "message": message},
    )
    _metrics["failed"] += 1
    _metrics["by_code"][code] = _metrics["by_code"].get(code, 0) + 1


async def _disk_guard_loop() -> None:
    """Warn when the work volume is nearly full (plan/009 T5.2)."""
    min_free_mb = int(os.getenv("DISK_MIN_FREE_MB", "5120"))
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            usage = shutil.disk_usage(str(WORK_DIR if WORK_DIR.exists() else Path("/")))
            free_mb = usage.free // (1024 * 1024)
            if free_mb < min_free_mb:
                logger.warning(
                    "disk low: %d MB free (threshold %d MB) at %s", free_mb, min_free_mb, WORK_DIR
                )
        except Exception as exc:
            logger.debug("disk guard failed: %s", exc)


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

# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class TranslateOptions(BaseModel):
    original_audio_volume_db: int = Field(default=-20, ge=-60, le=0)
    mute_original: bool = False
    subtitle_position: str = Field(default="bottom", pattern="^(bottom|top)$")
    remove_original_subtitles: bool = True
    burn_subtitles: bool = True
    subtitle_style: dict | None = None
    min_speed: float = Field(default=0.8, ge=0.5, le=1.0)
    max_speed: float = Field(default=1.3, ge=1.0, le=2.0)


class TranslateRequest(BaseModel):
    source_url: str | None = Field(default=None, min_length=8, max_length=2048)
    source_key: str | None = Field(default=None, min_length=1, max_length=1024)
    source_language: str = Field(default="auto", min_length=2, max_length=10)
    target_language: str = Field(default="vi", min_length=2, max_length=10)
    voice: str | None = Field(default=None, max_length=100)
    options: TranslateOptions = Field(default_factory=TranslateOptions)
    priority: int = Field(default=0, ge=-100, le=100)
    user_id: str | None = Field(default=None, max_length=64)
    job_id: str | None = Field(default=None, max_length=64)


class UploadPresignRequest(BaseModel):
    key: str = Field(..., min_length=1, max_length=1024)
    content_type: str | None = Field(default=None, max_length=100)
    content_length: int | None = Field(default=None, ge=1, le=5368709120)


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
    # `scheduler.alive` being false is the failure mode where the worker answers
    # health checks while every job stays queued forever — surface it loudly.
    try:
        scheduler_state = get_scheduler().snapshot()
    except Exception:
        scheduler_state = {"alive": False, "reason": "not initialised"}
    healthy = scheduler_state.get("alive", False)
    return {
        "status": "ok" if healthy else "degraded",
        "concurrency": CONCURRENCY,
        "work_dir": str(WORK_DIR),
        "signing_configured": bool(parse_keys(HMAC_KEYS_JSON)),
        "gemini_configured": bool(GEMINI_API_KEY),
        "scheduler": scheduler_state,
    }


@app.get("/metrics")
async def metrics():
    uptime = time.time() - _metrics["started_at"]
    avg = _metrics["total_duration"] / _metrics["requests"] if _metrics["requests"] else 0
    # queue depth (T5.1) + cache stats (T1.5) + breaker counts (T2.5)
    try:
        from job_queue import queue_depth as _qd

        qd = await _qd()
    except Exception:
        qd = None
    try:
        from cache import CACHE_DIR, hit_rate
        from cache import stats as cache_counters

        cache_files = len(list(CACHE_DIR.glob("*"))) if CACHE_DIR.exists() else 0
        cache_size = (
            sum(p.stat().st_size for p in CACHE_DIR.glob("*") if p.is_file())
            if CACHE_DIR.exists()
            else 0
        )
        cache_hit_rate = hit_rate()
        cache_stats = cache_counters()
    except Exception:
        cache_files = None
        cache_size = None
        cache_hit_rate = None
        cache_stats = {}
    try:
        import resilience

        breakers = {k: v.current_state for k, v in resilience._breakers.items()}
    except Exception:
        breakers = {}
    return {
        **_metrics,
        "uptime_seconds": round(uptime, 1),
        "avg_duration_seconds": round(avg, 2),
        "queue_depth": qd,
        "queue_wait_seconds": round(_metrics["total_wait"] / _metrics["started_jobs"], 2)
        if _metrics["started_jobs"]
        else 0,
        "cache_files": cache_files,
        "cache_size_bytes": cache_size,
        "cache_hit_rate": cache_hit_rate,
        "cache": cache_stats,
        "breakers": breakers,
        "cost_estimate_usd": round(_estimate_cost(), 4),
    }


def _estimate_cost() -> float:
    """Rough per-process estimate so operators see spend without a billing call.

    Gemini Flash pricing is per token and the worker does not receive usage
    metadata back through the SDK path we use, so this is a deliberately coarse
    per-completed-job estimate. It is a monitoring signal, not an invoice.
    """
    per_job = float(os.getenv("COST_ESTIMATE_PER_JOB_USD", "0.01"))
    return _metrics["success"] * per_job


@app.post("/uploads/presign")
async def create_upload_presign(req: UploadPresignRequest):
    """Presigned PUT for browser direct-to-storage upload (plan/009 T3.1)."""
    if not req.key.startswith("uploads/"):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_KEY", "message": "key must start with uploads/"},
        )
    try:
        import storage as s3

        url = await asyncio.to_thread(s3.presign_put, req.key, None, req.content_type)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500, detail={"code": "S3_NOT_CONFIGURED", "message": str(exc)}
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail={"code": "PRESIGN_FAILED", "message": str(exc)}
        ) from exc
    ttl = int(os.getenv("PRESIGN_TTL_SECONDS", "900"))
    return {"upload_url": url, "object_key": req.key, "key": req.key, "expires_in": ttl}


# ---------------------------------------------------------------------------
# Pipeline — stub (T1.x will fill each stage)
# ---------------------------------------------------------------------------


async def _process_job(job_id: str) -> None:
    """Run one job. Invoked by the scheduler (T5.1), never inline from a route."""
    start = time.time()
    _metrics["requests"] += 1
    job = await job_store.get_job(job_id)
    try:
        if job is None:
            return
        # queue wait (created -> started) for T5.3
        _metrics["started_jobs"] += 1
        _metrics["total_wait"] += max(0.0, start - job.created_at)
        # Local-file support for tests: copy into the work dir and mark the
        # download stage done so the pipeline skips it. Remote URLs go through
        # video-worker inside pipeline.run_pipeline.
        source_url = job.source_url
        if source_url:
            local: Path | None = None
            if source_url.startswith("file://"):
                local = Path(source_url[7:])
            elif Path(source_url).exists():
                local = Path(source_url)
            if local and local.exists():
                work = WORK_DIR / job_id
                work.mkdir(parents=True, exist_ok=True)
                dst = work / "source.mp4"
                if not dst.exists():
                    await asyncio.to_thread(shutil.copy2, local, dst)
                await job_store.update_job(
                    job_id, stage_state={**job.stage_state, "downloading": "done"}
                )
        import pipeline as pipeline_mod

        await pipeline_mod.run_pipeline(job_id)
        job = await job_store.get_job(job_id)
        if job and job.status == "completed":
            _metrics["success"] += 1
        else:
            _metrics["failed"] += 1
            code = (job.error or {}).get("code", "FAILED") if job else "FAILED"
            _metrics["by_code"][code] = _metrics["by_code"].get(code, 0) + 1
    except Exception as exc:
        _metrics["failed"] += 1
        code = getattr(exc, "code", "FAILED") if hasattr(exc, "code") else "FAILED"
        if isinstance(exc, str):
            code = exc
        _metrics["by_code"][code] = _metrics["by_code"].get(code, 0) + 1
        # run_pipeline already set job to failed, but ensure
        try:
            await job_store.update_job(
                job_id, status="failed", error={"code": code, "message": str(exc)}
            )
        except Exception:
            pass
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
        user_id=req.user_id,
        priority=req.priority,
        request_id=request_id,
        job_id=req.job_id,
    )
    # The scheduler picks this up by (priority DESC, created_at ASC) (T5.1).
    logger.info("job queued", extra={"job_id": job.job_id, "priority": job.priority})
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


@app.get("/presign")
async def presign_key(key: str):
    """Presign an object key directly (plan/009 §5.1).

    The API stores artifact keys, so it can still hand the browser a download URL
    after this worker restarts and loses its in-memory job. Only keys under the
    known prefixes are signable.
    """
    if not (key.startswith("results/") or key.startswith("uploads/")):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_KEY", "message": "key must start with results/ or uploads/"},
        )
    try:
        import storage as s3

        exists = await asyncio.to_thread(s3.exists, key)
        if not exists:
            raise HTTPException(
                status_code=404,
                detail={"code": "NOT_FOUND", "message": f"object not found: {key}"},
            )
        url = await asyncio.to_thread(s3.presign_get, key)
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500, detail={"code": "S3_NOT_CONFIGURED", "message": str(exc)}
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail={"code": "PRESIGN_FAILED", "message": str(exc)}
        ) from exc
    return {"url": url, "key": key, "expires_in": int(os.getenv("PRESIGN_TTL_SECONDS", "900"))}


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
    if format not in ("json", "md", "srt"):
        raise HTTPException(status_code=400, detail="format must be json, md or srt")
    work = WORK_DIR / job_id
    if format == "json":
        seg = work / "segments.json"
        if not seg.exists():
            raise HTTPException(status_code=404, detail="Transcript not ready")
        return JSONResponse(content=json.loads(seg.read_text(encoding="utf-8")))
    name = "transcript.md" if format == "md" else "transcript.srt"
    path = work / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Transcript not ready")
    media = (
        "text/markdown; charset=utf-8" if format == "md" else "application/x-subrip; charset=utf-8"
    )
    return Response(
        content=path.read_text(encoding="utf-8"),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


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
