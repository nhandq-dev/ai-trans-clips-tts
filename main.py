from __future__ import annotations

import asyncio
import hmac
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from tts_engine import engine_for, generate_tts
from voices import catalog

MAX_TEXT_LENGTH = int(os.getenv("MAX_TEXT_LENGTH", "5000"))
TTS_CONCURRENCY = int(os.getenv("TTS_CONCURRENCY", "2"))
OUTPUT_DIR = Path(os.getenv("TTS_OUTPUT_DIR", "tts_output"))
WORKER_SECRET = os.getenv("TTS_WORKER_SECRET", "")

app = FastAPI(title="TTS Worker", version="1.0.0")


class WorkerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        if not WORKER_SECRET:
            return JSONResponse(
                status_code=503, content={"error": "TTS_WORKER_SECRET not configured"}
            )
        provided = request.headers.get("X-Worker-Secret", "")
        if not hmac.compare_digest(provided, WORKER_SECRET):
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})
        return await call_next(request)


app.add_middleware(WorkerAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",")],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
_semaphore = asyncio.Semaphore(TTS_CONCURRENCY)


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_TEXT_LENGTH)
    language: str = Field(default="vi", max_length=20)
    voice: str | None = Field(default=None, max_length=100)
    format: str = Field(default="mp3")


class TTSResponse(BaseModel):
    engine: str
    language: str
    voice: str
    output_path: str


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "engines": {"vi": "vieneu", "other": "edge-tts"},
        "concurrency": TTS_CONCURRENCY,
    }


@app.get("/voices")
async def voices():
    return catalog()


@app.post("/tts", response_model=None)
async def tts(req: TTSRequest):
    fmt = req.format.lower().lstrip(".")
    if fmt not in ("mp3", "wav"):
        raise HTTPException(status_code=400, detail="format must be 'mp3' or 'wav'")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = OUTPUT_DIR / f"tts_{uuid.uuid4().hex}.{fmt}"

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


@app.post("/tts/meta", response_model=TTSResponse)
async def tts_meta(req: TTSRequest):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = OUTPUT_DIR / f"tts_{uuid.uuid4().hex}.{req.format.lower().lstrip('.')}"

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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8004")),
    )
