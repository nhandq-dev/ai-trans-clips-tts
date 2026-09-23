"""Stall detection tests (plan/009 §5.4).

A job must never sit in the queue without an explanation: the watchdog turns every
stall into an explicit error code so the UI can show something actionable.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jobs  # noqa: E402
import main  # noqa: E402
import scheduler as scheduler_module  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_store():
    jobs._jobs.clear()
    jobs._listeners.clear()
    yield
    jobs._jobs.clear()
    jobs._listeners.clear()


class _FakeScheduler:
    def __init__(self, alive: bool) -> None:
        self._alive = alive

    @property
    def alive(self) -> bool:
        return self._alive


def test_queued_job_past_timeout_is_failed_with_queue_timeout(monkeypatch):
    monkeypatch.setattr(main, "get_scheduler", lambda *_: _FakeScheduler(alive=True))

    async def scenario():
        job = await jobs.create_job(source_url="http://x/1")
        # Backdate it as if it has been waiting well past the limit.
        await jobs.update_job(job.job_id, created_at=time.time() - 5000)
        await main._reap_stalled_jobs(queue_timeout=60, job_timeout=3600)
        return await jobs.get_job(job.job_id)

    job = asyncio.run(scenario())
    assert job is not None
    assert job.status == "failed"
    assert job.error is not None
    assert job.error["code"] == "QUEUE_TIMEOUT"
    assert "queued" in job.error["message"]


def test_dead_scheduler_fails_queued_jobs_immediately(monkeypatch):
    monkeypatch.setattr(main, "get_scheduler", lambda *_: _FakeScheduler(alive=False))

    async def scenario():
        job = await jobs.create_job(source_url="http://x/1")
        await main._reap_stalled_jobs(queue_timeout=3600, job_timeout=3600)
        return await jobs.get_job(job.job_id)

    job = asyncio.run(scenario())
    assert job is not None
    assert job.status == "failed"
    assert job.error is not None
    assert job.error["code"] == "SCHEDULER_DOWN"
    assert "never" in job.error["message"]


def test_stuck_processing_job_is_failed_with_job_timeout(monkeypatch):
    monkeypatch.setattr(main, "get_scheduler", lambda *_: _FakeScheduler(alive=True))

    async def scenario():
        job = await jobs.create_job(source_url="http://x/1")
        await jobs.update_job(job.job_id, status="processing", stage="transcribing")
        # `update_job` always stamps `updated_at` with now, so backdate in place.
        stored = await jobs.get_job(job.job_id)
        assert stored is not None
        # Must stay below jobs.JOB_TTL_SECONDS or list_jobs() would expire it first.
        stored.updated_at = time.time() - 300
        await main._reap_stalled_jobs(queue_timeout=3600, job_timeout=60)
        return await jobs.get_job(job.job_id)

    job = asyncio.run(scenario())
    assert job is not None
    assert job.status == "failed"
    assert job.error is not None
    assert job.error["code"] == "JOB_TIMEOUT"


def test_healthy_job_is_left_alone(monkeypatch):
    monkeypatch.setattr(main, "get_scheduler", lambda *_: _FakeScheduler(alive=True))

    async def scenario():
        job = await jobs.create_job(source_url="http://x/1")
        await main._reap_stalled_jobs(queue_timeout=60, job_timeout=60)
        return await jobs.get_job(job.job_id)

    job = asyncio.run(scenario())
    assert job is not None
    assert job.status == "queued"
    assert job.error is None
    assert scheduler_module.CONCURRENCY >= 1
