"""Shared job store + queue for asynchronous TTS (plan/014 Phase 1).

The worker used to be synchronous only: `POST /v1/tts` held the connection until
synthesis finished, which capped requests at the client timeout (80 s) and pinned
one uvicorn worker. Async jobs decouple the two.

Redis backs the store in production because the worker runs N uvicorn processes:
a job created by one process must be visible to the process that polls it. When
`REDIS_URL` is unset the worker falls back to an in-process store, which is only
correct for a single process (local dev and tests).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("tts-worker.jobs")

JOB_QUEUED = "queued"
JOB_PROCESSING = "processing"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"

QUEUE_KEY = "tts:jobs:queue"
JOB_KEY_PREFIX = "tts:job:"


@dataclass
class TtsJob:
    """A synthesis request plus its progress. `text` is never returned to callers."""

    id: str
    status: str = JOB_QUEUED
    progress: int = 0
    stage: str = ""
    error: str | None = None
    text: str = ""
    language: str = "vi"
    voice: str | None = None
    fmt: str = "mp3"
    owner: int | None = None
    user_id: int | None = None
    audio_path: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def text_length(self) -> int:
        return len(self.text)

    def public(self) -> dict[str, Any]:
        """Safe representation: leaks neither the request text nor the disk path."""
        return {
            "job_id": self.id,
            "status": self.status,
            "progress": self.progress,
            "stage": self.stage,
            "error": self.error,
            "language": self.language,
            "voice": self.voice,
            "format": self.fmt,
            "text_length": self.text_length,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "has_audio": self.audio_path is not None and self.status == JOB_COMPLETED,
        }


def new_job(**kwargs: Any) -> TtsJob:
    now = time.time()
    return TtsJob(id=uuid.uuid4().hex, created_at=now, updated_at=now, **kwargs)


# --- field encoding: Redis hashes only hold strings ---------------------------

_FLOAT_FIELDS = {"created_at", "updated_at", "started_at", "finished_at"}
_INT_FIELDS = {"progress", "owner", "user_id"}


def _encode(job: TtsJob) -> dict[str, str]:
    return {
        "id": job.id,
        "status": job.status,
        "progress": str(job.progress),
        "stage": job.stage,
        "error": job.error or "",
        "text": job.text,
        "language": job.language,
        "voice": job.voice or "",
        "fmt": job.fmt,
        "owner": "" if job.owner is None else str(job.owner),
        "user_id": "" if job.user_id is None else str(job.user_id),
        "audio_path": job.audio_path or "",
        "created_at": f"{job.created_at:.6f}",
        "updated_at": f"{job.updated_at:.6f}",
        "started_at": "" if job.started_at is None else f"{job.started_at:.6f}",
        "finished_at": "" if job.finished_at is None else f"{job.finished_at:.6f}",
    }


def _to_float(value: str) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _to_int(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _decode(raw: dict[str, str]) -> TtsJob | None:
    if not raw or not raw.get("id"):
        return None
    return TtsJob(
        id=raw["id"],
        status=raw.get("status") or JOB_QUEUED,
        progress=_to_int(raw.get("progress", "")) or 0,
        stage=raw.get("stage", ""),
        error=raw.get("error") or None,
        text=raw.get("text", ""),
        language=raw.get("language", "vi"),
        voice=raw.get("voice") or None,
        fmt=raw.get("fmt", "mp3"),
        owner=_to_int(raw.get("owner", "")),
        user_id=_to_int(raw.get("user_id", "")),
        audio_path=raw.get("audio_path") or None,
        created_at=_to_float(raw.get("created_at", "")) or 0.0,
        updated_at=_to_float(raw.get("updated_at", "")) or 0.0,
        started_at=_to_float(raw.get("started_at", "")),
        finished_at=_to_float(raw.get("finished_at", "")),
    )


@runtime_checkable
class JobStore(Protocol):
    """Shared state for async jobs. Implementations must be safe across coroutines."""

    #: False when the store cannot be shared between worker processes.
    shared: bool

    async def create(self, job: TtsJob) -> TtsJob: ...

    async def get(self, job_id: str) -> TtsJob | None: ...

    async def update(self, job_id: str, **fields: Any) -> TtsJob | None: ...

    async def enqueue(self, job_id: str) -> None: ...

    async def dequeue(self, timeout: int = 5) -> str | None: ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...


class MemoryJobStore:
    """Single-process fallback: correct for local dev and tests only."""

    shared = False

    def __init__(self, ttl_seconds: int = 86400) -> None:
        self._jobs: dict[str, TtsJob] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._ttl = ttl_seconds

    async def create(self, job: TtsJob) -> TtsJob:
        self._jobs[job.id] = job
        return job

    async def get(self, job_id: str) -> TtsJob | None:
        return self._jobs.get(job_id)

    async def update(self, job_id: str, **fields: Any) -> TtsJob | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        for key, value in fields.items():
            if hasattr(job, key):
                setattr(job, key, value)
        job.updated_at = time.time()
        return job

    async def enqueue(self, job_id: str) -> None:
        await self._queue.put(job_id)

    async def dequeue(self, timeout: int = 5) -> str | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class RedisJobStore:
    """Redis hash per job plus a list used as the work queue.

    Updates write individual fields with `HSET` instead of read-modify-write, so a
    late progress callback cannot clobber the terminal status.
    """

    shared = True

    def __init__(self, url: str, ttl_seconds: int = 86400, queue_key: str = QUEUE_KEY) -> None:
        import redis.asyncio as redis  # imported lazily so the dep stays optional

        self._redis = redis.from_url(url, encoding="utf-8", decode_responses=True)
        self._ttl = ttl_seconds
        self._queue_key = queue_key

    def _key(self, job_id: str) -> str:
        return f"{JOB_KEY_PREFIX}{job_id}"

    async def create(self, job: TtsJob) -> TtsJob:
        key = self._key(job.id)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.hset(key, mapping=_encode(job))
            pipe.expire(key, self._ttl)
            await pipe.execute()
        return job

    async def get(self, job_id: str) -> TtsJob | None:
        raw = await self._redis.hgetall(self._key(job_id))
        return _decode(raw)

    async def update(self, job_id: str, **fields: Any) -> TtsJob | None:
        key = self._key(job_id)
        encoded: dict[str, str] = {}
        for name, value in fields.items():
            if name in _FLOAT_FIELDS:
                encoded[name] = "" if value is None else f"{float(value):.6f}"
            elif name in _INT_FIELDS:
                encoded[name] = "" if value is None else str(int(value))
            else:
                encoded[name] = "" if value is None else str(value)

        if not await self._redis.exists(key):
            return None
        await self._redis.hset(key, mapping=encoded)
        return await self.get(job_id)

    async def enqueue(self, job_id: str) -> None:
        await self._redis.lpush(self._queue_key, job_id)

    async def dequeue(self, timeout: int = 5) -> str | None:
        result = await self._redis.brpop(self._queue_key, timeout=timeout)
        if not result:
            return None
        return result[1]

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False

    async def close(self) -> None:
        try:
            await self._redis.aclose()
        except Exception:  # pragma: no cover - best effort on shutdown
            pass


_store: JobStore | None = None


def get_job_store() -> JobStore:
    """Process-wide store singleton, chosen from `REDIS_URL`."""
    global _store
    if _store is None:
        from app.core.config import get_settings

        settings = get_settings()
        if settings.redis_url:
            _store = RedisJobStore(settings.redis_url, settings.tts_job_ttl_seconds)
        else:
            if settings.tts_workers > 1:
                logger.error(
                    "tts_workers=%d but REDIS_URL is unset: async jobs are process-local "
                    "and will not be visible to other workers",
                    settings.tts_workers,
                )
            _store = MemoryJobStore(settings.tts_job_ttl_seconds)
    return _store


def reset_job_store() -> None:
    """Test helper: drop the process-wide singleton."""
    global _store
    _store = None
