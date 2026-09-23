"""Priority queue + FIFO fairness helpers (plan/009 T5.1).

The pipeline worker is single-process with ``PIPELINE_CONCURRENCY`` (1-2). Queued
jobs are ordered by ``(priority DESC, created_at ASC)``. ``priority`` is the
snapshot of ``processingPriority`` from ``effective-entitlements.ts`` at job
creation time. Fairness: per-user concurrent cap is enforced in the API layer;
here we only order the queue.

This module is intentionally thin — the real store is ``jobs.py``. Functions
here just sort/inspect.
"""

from __future__ import annotations

import jobs
from jobs import Job


def _sort_key(job: Job) -> tuple[int, float]:
    # higher priority first, so negate for ascending sort; FIFO for tie
    return (-(job.priority if hasattr(job, "priority") else 0), job.created_at)


async def list_queued_sorted() -> list[Job]:
    js = await jobs.list_jobs(limit=500)
    queued = [j for j in js if j.status in ("queued", "processing")]
    queued.sort(key=_sort_key)
    return queued


async def queue_depth() -> int:
    js = await jobs.list_jobs(limit=500)
    return sum(1 for j in js if j.status in ("queued", "processing"))


async def next_queued() -> Job | None:
    q = await list_queued_sorted()
    return q[0] if q else None
