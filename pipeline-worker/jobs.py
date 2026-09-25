"""Async job store for the pipeline worker.

In-memory with TTL. Mirrors the video-worker's store (``video-worker/jobs.py``)
so the VPS can run a single worker instance without a DB dependency. The API
(Vercel, stateless) only proxies — it never stores pipeline state.

Stages (see plan/009 §4.2):
    downloading → extracting → transcribing → detecting_subs → synthesizing
    → aligning → muxing → done

Resume is stage-level (§4.5): each stage writes its artifacts atomically
(.tmp → mv) before marking itself done in ``stage_state``. The orchestrator
skips stages whose artifacts already exist.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

JobStatus = Literal["queued", "processing", "completed", "failed", "canceled"]
Stage = Literal[
    "queued",
    "downloading",
    "extracting",
    "transcribing",
    "detecting_subs",
    "synthesizing",
    "aligning",
    "muxing",
    "done",
]

JOB_TTL_SECONDS = 3600  # keep job record for 1h
JOB_FILE_TTL_SECONDS = 3600  # keep output files for 1h

STAGE_ORDER: list[Stage] = [
    "queued",
    "downloading",
    "extracting",
    "transcribing",
    "detecting_subs",
    "synthesizing",
    "aligning",
    "muxing",
    "done",
]


@dataclass
class Job:
    job_id: str
    # input
    source_url: str | None = None
    source_key: str | None = None
    source_language: str = "auto"
    target_language: str = "vi"
    voice: str | None = None
    options: dict = field(default_factory=dict)
    idempotency_key: str | None = None
    user_id: str | None = None
    priority: int = 0
    # Per-plan concurrent job allowance (plan/015). The env ceiling still applies;
    # the scheduler uses the smaller of the two.
    max_concurrent_jobs: int | None = None
    # Plan cap on the source length; enforced after download (plan/015).
    max_duration_seconds: int | None = None
    # progress
    status: JobStatus = "queued"
    stage: Stage = "queued"
    stage_state: dict = field(default_factory=dict)  # stage -> done/partial
    progress: int = 0  # 0-100, for SSE
    error: dict | None = None  # {code, message}
    # artifacts (object keys after upload, local paths before)
    artifacts: dict | None = None  # {video:{key,size}, markdown:{key,size}, ...}
    duration_seconds: int | None = None
    segments_count: int | None = None
    file_path: str | None = None  # absolute local path when completed (pre-upload)
    file_name: str | None = None
    file_size: int | None = None
    request_id: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        # don't leak absolute paths
        d.pop("file_path", None)
        return d


_jobs: dict[str, Job] = {}
_lock = asyncio.Lock()
_listeners: dict[str, list[asyncio.Queue]] = {}  # job_id -> [queues for SSE]


def _now() -> float:
    return time.time()


async def create_job(
    *,
    source_url: str | None = None,
    source_key: str | None = None,
    source_language: str = "auto",
    target_language: str = "vi",
    voice: str | None = None,
    options: dict | None = None,
    idempotency_key: str | None = None,
    user_id: str | None = None,
    priority: int = 0,
    max_concurrent_jobs: int | None = None,
    max_duration_seconds: int | None = None,
    request_id: str | None = None,
    job_id: str | None = None,
) -> Job:
    jid = (job_id or uuid.uuid4().hex)[:64]
    job = Job(
        job_id=jid,
        source_url=source_url,
        source_key=source_key,
        source_language=source_language,
        target_language=target_language,
        voice=voice,
        options=options or {},
        priority=priority,
        idempotency_key=idempotency_key,
        user_id=user_id,
        max_concurrent_jobs=max_concurrent_jobs,
        max_duration_seconds=max_duration_seconds,
        request_id=request_id,
    )
    async with _lock:
        _jobs[jid] = job
        _listeners[jid] = []
    return job


async def find_by_idempotency_key(user_id: str | None, idempotency_key: str) -> Job | None:
    """Return a non-terminal job matching the idempotency key, if any."""
    async with _lock:
        for job in _jobs.values():
            if job.idempotency_key == idempotency_key and job.user_id == user_id:
                if job.status in ("queued", "processing", "completed"):
                    return job
        return None


async def get_job(job_id: str) -> Job | None:
    async with _lock:
        job = _jobs.get(job_id)
        if job and _now() - job.updated_at > JOB_TTL_SECONDS:
            await _remove_job(job_id)
            return None
        return job


async def list_jobs(limit: int = 100) -> list[Job]:
    async with _lock:
        stale = [jid for jid, j in _jobs.items() if _now() - j.updated_at > JOB_TTL_SECONDS]
        for jid in stale:
            await _remove_job(jid)
        return sorted(_jobs.values(), key=lambda j: j.created_at, reverse=True)[:limit]


async def _remove_job(job_id: str) -> None:
    job = _jobs.pop(job_id, None)
    if job and job.file_path:
        try:
            Path(job.file_path).unlink(missing_ok=True)
        except Exception:
            pass
    _listeners.pop(job_id, None)


async def update_job(job_id: str, **fields) -> Job | None:
    async with _lock:
        job = _jobs.get(job_id)
        if not job:
            return None
        for k, v in fields.items():
            setattr(job, k, v)
        job.updated_at = _now()
        for q in _listeners.get(job_id, []):
            try:
                q.put_nowait(job.to_dict())
            except asyncio.QueueFull:
                pass
        return job


def add_listener(job_id: str) -> asyncio.Queue:
    """Create a queue that receives job dicts on every update. Caller must remove it."""
    q: asyncio.Queue = asyncio.Queue(maxsize=32)
    _listeners.setdefault(job_id, []).append(q)
    return q


def remove_listener(job_id: str, q: asyncio.Queue) -> None:
    lst = _listeners.get(job_id)
    if lst and q in lst:
        lst.remove(q)


async def cleanup_expired_files(output_dir: Path) -> None:
    """Remove output files older than JOB_FILE_TTL_SECONDS. Called periodically."""
    now = _now()
    for p in output_dir.glob("*"):
        if p.is_file() and p.name != ".cache":
            try:
                if now - p.stat().st_mtime > JOB_FILE_TTL_SECONDS:
                    p.unlink(missing_ok=True)
            except Exception:
                pass
