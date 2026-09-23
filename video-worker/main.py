"""Video download worker.

FastAPI service that downloads watermark-free video from an allowlisted set of
platforms (YouTube, Instagram, Facebook, TikTok, Douyin, Pinterest, Bilibili)
and streams the result back to the caller. Private to the API tier: every route
except /health requires HMAC-signed `X-Video-*` headers (see auth.py).

Follows the tts-worker conventions in this monorepo (auth middleware, health
endpoint, concurrency semaphore, BackgroundTask cleanup).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

import douyin
import jobs as job_store
from auth import HMACAuthMiddleware, RequestContextMiddleware
from dotenv import load_dotenv
from downloader import download
from errors import DownloadError, classify
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from platforms import platform_of, validate_url
from pydantic import BaseModel, Field
from security import parse_keys
from starlette.background import BackgroundTask

try:
    import storage as s3_storage

    _S3_ENABLED = bool(os.getenv("S3_BUCKET") and os.getenv("S3_ACCESS_KEY_ID"))
except Exception:  # storage not installed
    s3_storage = None  # type: ignore
    _S3_ENABLED = False

# Docker passes config through compose's env_file; running uvicorn directly
# (local dev) needs the .env loaded explicitly. Existing environment variables
# win, so a container override still takes precedence.
load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

HMAC_KEYS_JSON = os.getenv("HMAC_KEYS_JSON", "")
CONCURRENCY = int(os.getenv("VIDEO_CONCURRENCY", "2"))
INFO_TIMEOUT = int(os.getenv("VIDEO_INFO_TIMEOUT_SECONDS", "60"))
OUTPUT_DIR = Path(os.getenv("VIDEO_OUTPUT_DIR") or (Path(__file__).parent / "output"))
YTDLP_BASE = [sys.executable, "-m", "yt_dlp"]

_metrics: dict = {
    "requests": 0,
    "success": 0,
    "failed": 0,
    "cache_hits": 0,
    "by_platform": {},  # platform -> count
    "by_code": {},  # error code -> count
    "total_duration": 0.0,
    "started_at": time.time(),
}

CLEANUP_INTERVAL_SECONDS = int(os.getenv("VIDEO_CLEANUP_INTERVAL_SECONDS", "600"))

logger = logging.getLogger("video-worker")


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    """Periodically drop output files nobody downloaded.

    Files are normally deleted right after they are streamed (BackgroundTask);
    this sweeps the ones that were never fetched (job abandoned, client gone).
    Cache files are managed separately by their own TTL.
    """
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
            await job_store.cleanup_expired_files(OUTPUT_DIR)
        except Exception as exc:  # never let cleanup kill the loop
            logger.warning("cleanup failed: %s", exc)


app = FastAPI(title="Video Worker", version="1.0.0", lifespan=lifespan)


# RequestContextMiddleware is a BaseHTTPMiddleware (cheap, header-only); the
# HMAC check is a pure ASGI middleware so it can buffer and replay the body.
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


class DownloadRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2048)
    job_id: str | None = Field(default=None, max_length=64)


def _cleanup(path: Path) -> None:
    """Delete a finished output file. Cache files are owned by the dedupe store
    and must survive — deleting one would break every later request for that URL."""
    p = Path(path)
    if ".cache" in p.parts:
        return
    p.unlink(missing_ok=True)


async def _probe_info(url: str) -> dict:
    proc = await asyncio.create_subprocess_exec(
        *YTDLP_BASE,
        "--dump-json",
        "--no-playlist",
        "--no-warnings",
        "--skip-download",
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=INFO_TIMEOUT)
    except TimeoutError as exc:
        proc.kill()
        raise DownloadError("TIMEOUT") from exc
    if proc.returncode != 0:
        text = (stderr or b"").decode(errors="replace")
        raise DownloadError(classify(text), text[-500:])
    data = json.loads(stdout.decode(errors="replace").splitlines()[0])
    return {
        "title": data.get("title"),
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail"),
        "uploader": data.get("uploader"),
        "ext": data.get("ext"),
        "platform": platform_of(url),
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "concurrency": CONCURRENCY,
        "platforms": True,
        "signing_configured": bool(parse_keys(HMAC_KEYS_JSON)),
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


@app.get("/info")
async def info(url: str):
    """Verify a link and return whatever metadata we can.

    Metadata is best-effort: some platforms (notably Douyin) block the yt-dlp
    probe because their API needs signed requests, yet the download itself
    succeeds through the fallback chain. Only genuinely invalid or unsupported
    links are rejected here — the download reports the real failure otherwise.
    """
    try:
        validate_url(url)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "UNSUPPORTED", "message": str(exc)},
        ) from exc

    platform = platform_of(url)

    # Douyin's yt-dlp extractor always needs signed requests and fresh cookies,
    # so a plain probe just wastes 40-60s before failing. Short-circuit it
    # and return best-effort metadata so the UI's "verify" step is instant.
    if platform == "douyin":
        async with _semaphore:
            info: dict = {"platform": platform, "title": None}
            aweme_id = await asyncio.to_thread(douyin.resolve_aweme_id, url)
            meta = None
            if aweme_id:
                meta = await asyncio.to_thread(douyin.fetch_metadata, aweme_id)
            if not meta:
                meta = await asyncio.to_thread(douyin.fetch_metadata_public, url)
            if meta:
                info.update(meta)
            return info

    async with _semaphore:
        try:
            return await _probe_info(url)
        except DownloadError as exc:
            if exc.code in ("INVALID_URL", "UNSUPPORTED"):
                raise HTTPException(
                    status_code=400,
                    detail={"code": exc.code, "message": exc.message},
                ) from exc
            return {"platform": platform, "title": None}


@app.post("/download")
async def download_video(req: DownloadRequest):
    """Synchronous download — kept for backwards compat and fast platforms.

    For Douyin and other slow platforms, prefer POST /jobs (async).
    """
    try:
        validate_url(req.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job_id = (req.job_id or uuid.uuid4().hex)[:64]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    platform = platform_of(req.url) or "unknown"
    start = time.time()
    _metrics["requests"] += 1
    _metrics["by_platform"][platform] = _metrics["by_platform"].get(platform, 0) + 1

    async with _semaphore:
        try:
            result = await download(req.url, job_id)
        except DownloadError as exc:
            _metrics["failed"] += 1
            _metrics["by_code"][exc.code] = _metrics["by_code"].get(exc.code, 0) + 1
            _metrics["total_duration"] += time.time() - start
            raise HTTPException(
                status_code=exc.http_status,
                detail={"code": exc.code, "message": exc.message},
            ) from exc

    _metrics["success"] += 1
    _metrics["total_duration"] += time.time() - start
    if result.from_cache:
        _metrics["cache_hits"] += 1

    # Only delete files we created; cache files are reused across requests.
    background = None if result.from_cache else BackgroundTask(_cleanup, result.path)
    return FileResponse(
        path=str(result.path),
        media_type="video/mp4",
        filename=result.filename,
        background=background,
    )


@app.get("/audio")
async def get_audio(url: str):
    """Download and return the audio track only (16 kHz mono FLAC).

    Optional convenience for a text-only translation flow (plan/009 T5.5). The
    video is still fetched and muxed by yt-dlp first because that is the only
    reliable path across platforms; the extraction itself is a cheap local pass.
    """
    try:
        validate_url(url)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "UNSUPPORTED", "message": str(exc)}
        ) from exc

    job_id = uuid.uuid4().hex[:32]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    start = time.time()
    _metrics["requests"] += 1

    async with _semaphore:
        try:
            result = await download(url, job_id)
        except DownloadError as exc:
            _metrics["failed"] += 1
            _metrics["by_code"][exc.code] = _metrics["by_code"].get(exc.code, 0) + 1
            raise HTTPException(
                status_code=exc.http_status,
                detail={"code": exc.code, "message": exc.message},
            ) from exc

        audio_path = OUTPUT_DIR / f"{job_id}.flac"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            str(result.path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "flac",
            "-f",
            "flac",
            str(audio_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            audio_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "NO_AUDIO_TRACK",
                    "message": (stderr or b"").decode(errors="replace")[-400:],
                },
            )

    _metrics["success"] += 1
    _metrics["total_duration"] += time.time() - start

    return FileResponse(
        path=str(audio_path),
        media_type="audio/flac",
        filename=f"{job_id}.flac",
        background=BackgroundTask(_cleanup, audio_path),
    )


# ---------------------------------------------------------------------------
# Async jobs — for slow platforms and to avoid Vercel 60s timeout
# ---------------------------------------------------------------------------


class CreateJobRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2048)
    job_id: str | None = Field(default=None, max_length=64)


async def _process_job(job_id: str, url: str) -> None:
    await job_store.update_job(job_id, status="processing", progress=10)
    platform = platform_of(url) or "unknown"
    start = time.time()
    _metrics["requests"] += 1
    _metrics["by_platform"][platform] = _metrics["by_platform"].get(platform, 0) + 1
    try:
        async with _semaphore:
            result = await download(url, job_id)
        # T0.4: upload to object storage when configured so Vercel can redirect
        # (this is what keeps the download under the 4.5 MB function limit)
        s3_key = None
        if _S3_ENABLED and s3_storage and not result.from_cache and result.path.exists():
            try:
                key = f"results/video/{job_id}/{result.filename or result.path.name}"
                s3_key = await asyncio.to_thread(
                    s3_storage.upload_file, result.path, key, "video/mp4"
                )
                logger.info("uploaded to s3 key=%s size=%d", s3_key, result.path.stat().st_size)
            except Exception as exc:
                logger.warning("s3 upload failed, falling back to local file: %s", exc)
                s3_key = None
        elif _S3_ENABLED and result.from_cache and result.path.exists():
            # cache files are shared — still upload for presigned access if not yet in s3
            try:
                key = f"cache/video/{result.path.name}"
                if not await asyncio.to_thread(s3_storage.exists, key):  # type: ignore[union-attr]
                    await asyncio.to_thread(s3_storage.upload_file, result.path, key, "video/mp4")
                s3_key = key
                logger.info("s3 cache key=%s", s3_key)
            except Exception as exc:
                logger.warning("s3 cache upload failed: %s", exc)
        await job_store.update_job(
            job_id,
            status="completed",
            progress=100,
            file_path=str(result.path),
            file_name=result.filename,
            file_size=result.path.stat().st_size if result.path.exists() else None,
            from_cache=result.from_cache,
            s3_key=s3_key,
        )
        _metrics["success"] += 1
        _metrics["total_duration"] += time.time() - start
        if result.from_cache:
            _metrics["cache_hits"] += 1
    except DownloadError as exc:
        _metrics["failed"] += 1
        _metrics["by_code"][exc.code] = _metrics["by_code"].get(exc.code, 0) + 1
        _metrics["total_duration"] += time.time() - start
        await job_store.update_job(
            job_id, status="failed", progress=100, error={"code": exc.code, "message": exc.message}
        )
    except Exception as exc:
        _metrics["failed"] += 1
        _metrics["by_code"]["FAILED"] = _metrics["by_code"].get("FAILED", 0) + 1
        _metrics["total_duration"] += time.time() - start
        await job_store.update_job(
            job_id, status="failed", progress=100, error={"code": "FAILED", "message": str(exc)}
        )


@app.post("/jobs", status_code=202)
async def create_job(req: CreateJobRequest):
    try:
        validate_url(req.url)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "UNSUPPORTED", "message": str(exc)}
        ) from exc
    platform = platform_of(req.url)
    job = await job_store.create_job(req.url, platform, req.job_id)
    # Don't await — process in background
    asyncio.create_task(_process_job(job.job_id, req.url))
    return job.to_dict()


@app.get("/jobs/{job_id}")
async def get_job_status(job_id: str):
    job = await job_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@app.get("/jobs/{job_id}/file")
async def get_job_file(job_id: str):
    job = await job_store.get_job(job_id)
    if not job or job.status != "completed":
        raise HTTPException(status_code=404, detail="File not ready")
    # T0.4: if object storage is enabled and we have a key, redirect to presigned URL
    # so the download bypasses Vercel (4.5MB limit). The API will also use this.
    if _S3_ENABLED and s3_storage and getattr(job, "s3_key", None):
        try:
            url = await asyncio.to_thread(s3_storage.presign_get, job.s3_key)  # type: ignore[union-attr]
            return RedirectResponse(url=url, status_code=302)
        except Exception as exc:
            logger.warning("presign failed key=%s: %s", job.s3_key, exc)
    # Fallback: stream from local disk (dev or when S3 not configured)
    if not job.file_path:
        raise HTTPException(status_code=404, detail="File not ready")
    path = Path(job.file_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")
    background = None if job.from_cache else BackgroundTask(_cleanup, path)
    return FileResponse(
        path=str(path),
        media_type="video/mp4",
        filename=job.file_name or path.name,
        background=background,
    )


@app.get("/jobs/{job_id}/presign")
async def get_job_presign(job_id: str):
    """Worker signs the URL itself (plan/009 §5.1) — the API just forwards it."""
    job = await job_store.get_job(job_id)
    if not job or job.status != "completed" or not getattr(job, "s3_key", None):
        raise HTTPException(status_code=404, detail="File not ready or not in object storage")
    try:
        url = await asyncio.to_thread(s3_storage.presign_get, job.s3_key)  # type: ignore[union-attr]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "url": url,
        "key": job.s3_key,
        "expires_in": int(os.getenv("PRESIGN_TTL_SECONDS", "900")),
    }


@app.get("/jobs/{job_id}/events")
async def job_events(job_id: str):
    job = await job_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_gen():
        # Send current state immediately
        current = await job_store.get_job(job_id)
        if current:
            yield f"data: {json.dumps(current.to_dict())}\n\n"
            if current.status in ("completed", "failed"):
                return
        # Then stream updates
        q = job_store.add_listener(job_id)
        try:
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=30)
                    yield f"data: {json.dumps(data)}\n\n"
                    if data.get("status") in ("completed", "failed"):
                        break
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            job_store.remove_listener(job_id, q)

    from fastapi.responses import StreamingResponse

    return StreamingResponse(event_gen(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8005")),
    )
