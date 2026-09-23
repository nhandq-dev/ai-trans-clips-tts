"""Priority scheduler for queued jobs (plan/009 T5.1, T5.7).

A single dispatcher loop picks the next runnable job by
``(priority DESC, created_at ASC)`` and starts it while a concurrency slot is
free. Fairness is enforced per user: one user cannot occupy every slot.

Jobs are only started here — ``main.py`` never runs a pipeline inline, so a
burst of submissions cannot bypass ordering.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

import jobs
from jobs import Job

logger = logging.getLogger("pipeline-worker.scheduler")

CONCURRENCY = int(os.getenv("PIPELINE_CONCURRENCY", "1"))
MAX_CONCURRENT_PER_USER = int(os.getenv("MAX_CONCURRENT_PER_USER", "2"))
POLL_INTERVAL_SECONDS = float(os.getenv("SCHEDULER_POLL_SECONDS", "1"))

Runner = "Callable[[str], Awaitable[None]]"


class Scheduler:
    def __init__(
        self,
        runner,
        concurrency: int | None = None,
        max_concurrent_per_user: int | None = None,
    ) -> None:
        self._runner = runner
        self._concurrency = CONCURRENCY if concurrency is None else concurrency
        self._max_per_user = (
            MAX_CONCURRENT_PER_USER if max_concurrent_per_user is None else max_concurrent_per_user
        )
        self._tasks: dict[str, asyncio.Task] = {}
        self._running_per_user: dict[str, int] = {}
        self._loop_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._stop.clear()
        self._loop_task = asyncio.create_task(self._dispatch_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._loop_task:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @property
    def running_count(self) -> int:
        return len(self._tasks)

    async def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._dispatch_once()
            except Exception as exc:  # never let the loop die
                logger.warning("scheduler dispatch failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL_SECONDS)
            except TimeoutError:
                pass

    async def _dispatch_once(self) -> None:
        if len(self._tasks) >= self._concurrency:
            return

        candidates = await jobs.list_jobs(limit=500)
        queued = [j for j in candidates if j.status == "queued"]
        # priority DESC, then FIFO
        queued.sort(key=lambda j: (-(j.priority or 0), j.created_at))

        for job in queued:
            if len(self._tasks) >= self._concurrency:
                return
            if job.job_id in self._tasks:
                continue
            user_key = str(job.user_id or "anon")
            if self._running_per_user.get(user_key, 0) >= self._max_per_user:
                continue
            self._start(job)

    def _start(self, job: Job) -> None:
        user_key = str(job.user_id or "anon")
        self._running_per_user[user_key] = self._running_per_user.get(user_key, 0) + 1
        task = asyncio.create_task(self._run(job.job_id, user_key))
        self._tasks[job.job_id] = task

    async def _run(self, job_id: str, user_key: str) -> None:
        try:
            await self._runner(job_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("job %s crashed: %s", job_id, exc)
        finally:
            self._tasks.pop(job_id, None)
            remaining = self._running_per_user.get(user_key, 1) - 1
            if remaining <= 0:
                self._running_per_user.pop(user_key, None)
            else:
                self._running_per_user[user_key] = remaining


_scheduler: Scheduler | None = None


def get_scheduler(runner=None) -> Scheduler:
    global _scheduler
    if _scheduler is None:
        if runner is None:
            raise RuntimeError("scheduler not initialised")
        _scheduler = Scheduler(runner)
    return _scheduler
