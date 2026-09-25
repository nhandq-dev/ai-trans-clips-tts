"""Async TTS job consumer (plan/014 Phase 1).

One consumer runs per uvicorn worker, so the number of concurrent syntheses still
equals `TTS_WORKERS` — the same lanes as the synchronous path, just decoupled from
the HTTP request. `generate_tts` reports progress through a callback, which is
piped into the job store so the client can show a progress bar.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.services import custom_voices
from app.services.job_store import (
    JOB_COMPLETED,
    JOB_FAILED,
    JOB_PROCESSING,
    JOB_QUEUED,
    JobStore,
    TtsJob,
)
from app.services.storage import output_dir
from app.services.synthesis import generate_tts

logger = logging.getLogger("tts-worker.jobs")

ERROR_MAX_CHARS = 500
QUEUE_POLL_SECONDS = 5


async def process_job(store: JobStore, job_id: str) -> None:
    """Run one queued job to completion, mirroring its progress into the store."""
    job: TtsJob | None = await store.get(job_id)
    if job is None:
        logger.warning("job_missing job=%s", job_id)
        return
    if job.status != JOB_QUEUED:
        logger.warning("job_not_queued job=%s status=%s", job_id, job.status)
        if job.tier:
            await store.remove_active(job.tier, job_id)
        return

    try:
        await _run_job(store, job, job_id)
    finally:
        # The Free-pool cap counts queued + processing, so the slot is released as
        # soon as the job reaches a terminal state — including a crash here.
        if job.tier:
            await store.remove_active(job.tier, job_id)


async def _run_job(store: JobStore, job: TtsJob, job_id: str) -> None:
    await store.update(
        job_id,
        status=JOB_PROCESSING,
        started_at=time.time(),
        progress=1,
        stage="starting",
    )

    loop = asyncio.get_running_loop()
    furthest = 0

    def on_progress(percent: int, stage: str) -> None:
        # Called from the synthesis thread. Progress must never go backwards, and a
        # late callback must not overwrite the terminal status, so only forward
        # advances are written (RedisJobStore updates single fields).
        nonlocal furthest
        if percent <= furthest:
            return
        furthest = percent
        asyncio.run_coroutine_threadsafe(
            store.update(job_id, progress=percent, stage=stage),
            loop,
        )

    dest = output_dir() / f"tts_{job_id}.{job.fmt}"
    try:
        voice_data = None
        if job.voice and job.owner is not None:
            voice_data = await asyncio.to_thread(custom_voices.load_voice, job.owner, job.voice)

        path = await asyncio.to_thread(
            generate_tts,
            job.text,
            job.language,
            job.voice,
            dest,
            job.fmt,
            on_progress,
            voice_data,
        )
        await store.update(
            job_id,
            status=JOB_COMPLETED,
            progress=100,
            stage="completed",
            audio_path=str(path),
            finished_at=time.time(),
            error=None,
        )
        logger.info("job_completed job=%s chars=%d", job_id, job.text_length)
    except Exception as exc:  # noqa: BLE001 - report any failure to the caller
        logger.exception("job_failed job=%s", job_id)
        await store.update(
            job_id,
            status=JOB_FAILED,
            stage="failed",
            error=str(exc)[:ERROR_MAX_CHARS] or exc.__class__.__name__,
            finished_at=time.time(),
        )


async def consume_forever(store: JobStore) -> None:
    """Blocking-pop loop. Cancelled on shutdown by the lifespan handler."""
    logger.info("job_consumer_started")
    while True:
        try:
            job_id = await store.dequeue(timeout=QUEUE_POLL_SECONDS)
        except asyncio.CancelledError:
            logger.info("job_consumer_stopped")
            raise
        except Exception:
            logger.exception("job_queue_unavailable")
            await asyncio.sleep(QUEUE_POLL_SECONDS)
            continue
        if job_id:
            await process_job(store, job_id)
