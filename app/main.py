from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app.api.routes import custom_voices, health, jobs, tts, voices
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import DocsAuthMiddleware, HMACAuthMiddleware, RequestContextMiddleware
from app.services import job_runner
from app.services.job_store import get_job_store
from app.services.synthesis import warm_up

settings = get_settings()
configure_logging(settings)

logger = logging.getLogger("tts-worker")

if settings.hf_home is not None:
    os.environ.setdefault("HF_HOME", str(settings.hf_home))


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.readiness_warmup:
        logger.info("warmup_started")
        try:
            await asyncio.to_thread(warm_up)
            logger.info("warmup_finished")
        except Exception:
            logger.exception("warmup_failed")

    # One consumer per uvicorn worker: the number of concurrent syntheses equals
    # TTS_WORKERS, matching the synchronous path's lanes.
    consumer: asyncio.Task | None = None
    store = get_job_store()
    if settings.tts_jobs_consumer_enabled:
        if not await store.ping():
            logger.error("job_store_unavailable: async TTS jobs will not be processed")
        consumer = asyncio.create_task(job_runner.consume_forever(store))
    try:
        yield
    finally:
        if consumer is not None:
            consumer.cancel()
            with suppress(asyncio.CancelledError):
                await consumer
        await store.close()


app = FastAPI(
    title="TTS Worker",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None if settings.docs_disabled else "/docs",
    redoc_url=None if settings.docs_disabled else "/redoc",
    openapi_url=None if settings.docs_disabled else "/openapi.json",
)

register_exception_handlers(app)
app.add_middleware(HMACAuthMiddleware, settings=settings)
app.add_middleware(DocsAuthMiddleware, settings=settings)
app.add_middleware(RequestContextMiddleware, settings=settings)

app.include_router(health.router)
app.include_router(voices.router)
app.include_router(custom_voices.router)
app.include_router(tts.router)
app.include_router(jobs.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port)
