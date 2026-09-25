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


def test_plan_concurrency_limit_narrows_the_per_user_ceiling():
    """A Free job carries max_concurrent_jobs=1, so it cannot take two slots."""

    async def runner(job_id: str) -> None:
        await asyncio.sleep(0.2)

    async def scenario():
        # Box ceiling is 2, but this user's plan allows only 1.
        scheduler = Scheduler(runner, concurrency=10, max_concurrent_per_user=2)
        free1 = await jobs.create_job(source_url="http://a/1", user_id="u1", max_concurrent_jobs=1)
        free2 = await jobs.create_job(source_url="http://a/2", user_id="u1", max_concurrent_jobs=1)
        paid1 = await jobs.create_job(source_url="http://a/3", user_id="u2", max_concurrent_jobs=2)
        paid2 = await jobs.create_job(source_url="http://a/4", user_id="u2", max_concurrent_jobs=2)
        await scheduler._dispatch_once()
        running = set(scheduler._tasks.keys())
        for task in list(scheduler._tasks.values()):
            task.cancel()
        return free1, free2, paid1, paid2, running

    free1, free2, paid1, paid2, running = asyncio.run(scenario())
    assert len(running) == 3
    # Free (1): exactly one of its two jobs.
    assert len({free1.job_id, free2.job_id} & running) == 1
    # Pro (2): both run.
    assert {paid1.job_id, paid2.job_id} <= running


def test_plan_limit_cannot_exceed_the_env_ceiling():
    async def runner(job_id: str) -> None:
        await asyncio.sleep(0.2)

    async def scenario():
        scheduler = Scheduler(runner, concurrency=10, max_concurrent_per_user=1)
        await jobs.create_job(source_url="http://a/1", user_id="u1", max_concurrent_jobs=3)
        await jobs.create_job(source_url="http://a/2", user_id="u1", max_concurrent_jobs=3)
        await scheduler._dispatch_once()
        running = set(scheduler._tasks.keys())
        for task in list(scheduler._tasks.values()):
            task.cancel()
        return running

    assert len(asyncio.run(scenario())) == 1


def test_missing_plan_limit_falls_back_to_the_env_ceiling():
    async def runner(job_id: str) -> None:
        await asyncio.sleep(0.2)

    async def scenario():
        scheduler = Scheduler(runner, concurrency=10, max_concurrent_per_user=2)
        await jobs.create_job(source_url="http://a/1", user_id="u1")
        await jobs.create_job(source_url="http://a/2", user_id="u1")
        await scheduler._dispatch_once()
        running = set(scheduler._tasks.keys())
        for task in list(scheduler._tasks.values()):
            task.cancel()
        return running

    assert len(asyncio.run(scenario())) == 2


def test_dispatch_loop_survives_the_poll_timeout(monkeypatch):
    """Regression: the loop used to die on the first idle poll.

    On Python < 3.11 ``asyncio.wait_for`` raises ``asyncio.TimeoutError``, which is
    NOT the builtin ``TimeoutError``. Catching the builtin let the exception escape
    ``_dispatch_loop`` and kill the scheduler silently: the worker stayed healthy
    while every job sat in ``queued`` forever.
    """
    import contextlib

    import scheduler as scheduler_module
    from scheduler import Scheduler

    monkeypatch.setattr(scheduler_module, "POLL_INTERVAL_SECONDS", 0.01)

    async def scenario():
        started: list[str] = []

        async def runner(job_id: str) -> None:
            started.append(job_id)

        sched = Scheduler(runner, concurrency=5, max_concurrent_per_user=5)
        loop = asyncio.create_task(sched._dispatch_loop())

        # Idle for a few poll cycles first: this is where the old code crashed.
        await asyncio.sleep(0.05)
        assert not loop.done(), "dispatch loop died on an idle poll"

        job = await jobs.create_job(source_url="http://x/1")
        for _ in range(50):
            if started:
                break
            await asyncio.sleep(0.02)

        sched._stop.set()
        loop.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop
        return started, job.job_id

    started, job_id = asyncio.run(scenario())
    assert job_id in started, "queued job was never dispatched after the loop survived"
