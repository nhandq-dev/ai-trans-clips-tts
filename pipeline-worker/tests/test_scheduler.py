"""Tests for the priority scheduler (plan/009 T5.1)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jobs  # noqa: E402
from scheduler import Scheduler  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_store():
    jobs._jobs.clear()
    jobs._listeners.clear()
    yield
    jobs._jobs.clear()
    jobs._listeners.clear()


def test_dispatches_in_priority_then_fifo_order():
    started: list[str] = []
    order: list[str] = []

    async def runner(job_id: str) -> None:
        started.append(job_id)
        order.append(job_id)

    async def scenario():
        scheduler = Scheduler(runner, concurrency=10, max_concurrent_per_user=10)
        low = await jobs.create_job(source_url="http://a/1", priority=0)
        await asyncio.sleep(0.01)
        high = await jobs.create_job(source_url="http://a/2", priority=10)
        await asyncio.sleep(0.01)
        mid = await jobs.create_job(source_url="http://a/3", priority=5)
        await scheduler._dispatch_once()
        await asyncio.gather(*scheduler._tasks.values())
        return low, high, mid

    low, high, mid = asyncio.run(scenario())
    assert order == [high.job_id, mid.job_id, low.job_id]


def test_respects_per_user_concurrency():
    async def runner(job_id: str) -> None:
        await asyncio.sleep(0.2)

    async def scenario():
        scheduler = Scheduler(runner, concurrency=10, max_concurrent_per_user=1)
        a1 = await jobs.create_job(source_url="http://a/1", user_id="u1", priority=0)
        a2 = await jobs.create_job(source_url="http://a/2", user_id="u1", priority=0)
        b1 = await jobs.create_job(source_url="http://a/3", user_id="u2", priority=0)
        await scheduler._dispatch_once()
        running = set(scheduler._tasks.keys())
        for task in list(scheduler._tasks.values()):
            task.cancel()
        return a1, a2, b1, running

    a1, a2, b1, running = asyncio.run(scenario())
    # one slot for u1 and one for u2 -> a2 waits
    assert b1.job_id in running
    assert len(running) == 2
    assert a1.job_id in running or a2.job_id in running
