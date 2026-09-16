from __future__ import annotations

import hmac
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.routes import health, tts, voices
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware

settings = get_settings()
configure_logging(settings)

if settings.hf_home is not None:
    os.environ.setdefault("HF_HOME", str(settings.hf_home))


class WorkerAuthMiddleware(BaseHTTPMiddleware):
    """Transitional static-secret auth; replaced by HMAC signing in T05."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        if not settings.tts_worker_secret:
            return JSONResponse(
                status_code=503, content={"error": "TTS_WORKER_SECRET not configured"}
            )
        provided = request.headers.get("X-Worker-Secret", "")
        if not hmac.compare_digest(provided, settings.tts_worker_secret):
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})
        return await call_next(request)


app = FastAPI(
    title="TTS Worker",
    version="1.0.0",
    docs_url=None if settings.docs_disabled else "/docs",
    redoc_url=None if settings.docs_disabled else "/redoc",
    openapi_url=None if settings.docs_disabled else "/openapi.json",
)

register_exception_handlers(app)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_allow_origins.split(",")],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(WorkerAuthMiddleware)
app.add_middleware(RequestContextMiddleware, settings=settings)

app.include_router(health.router)
app.include_router(voices.router)
app.include_router(tts.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port)
