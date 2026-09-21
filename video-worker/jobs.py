"""Async job store for the video worker.

Jobs are in-memory with TTL. The VPS runs a single worker instance, so an
in-memory store is sufficient and avoids a DB dependency. The API (Vercel,
stateless) proxies to the worker — it never stores jobs itself.

Lifecycle: queued -> processing -> completed | failed
Files are kept for JOB_FILE_TTL_SECONDS after completion, then cleaned.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

JobStatus = Literal["queued", "processing", "completed", "failed"]

JOB_TTL_SECONDS = 3600  # keep job record for 1h
JOB_FILE_TTL_SECONDS = 3600  # keep output file for 1h


@dataclass
class Job:
    job_id: str
    url: str
    status: JobStatus = "queued"
    platform: str | None = None
    progress: int = 0  # 0-100, for SSE
    error: dict | None = None  # {code, message}
    file_path: str | None = None  # absolute path when completed
    file_name: str | None = None
    file_size: int | None = None
    from_cache: bool = False  # cache files must not be deleted after streaming
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


async def create_job(url: str, platform: str | None, job_id: str | None = None) -> Job:
    jid = (job_id or uuid.uuid4().hex)[:64]
    job = Job(job_id=jid, url=url, platform=platform)
    async with _lock:
        _jobs[jid] = job
        _listeners[jid] = []
    return job


async def get_job(job_id: str) -> Job | None:
    async with _lock:
        job = _jobs.get(job_id)
        # lazy expire
        if job and _now() - job.updated_at > JOB_TTL_SECONDS:
            await _remove_job(job_id)
            return None
        return job


async def list_jobs(limit: int = 100) -> list[Job]:
    async with _lock:
        # expire stale
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
        # notify SSE listeners
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
    # also clean cache is handled in downloader._find_cached
