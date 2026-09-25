"""Redis-backed job store (the production path).

Skipped when no Redis is reachable, so CI without a Redis service still passes.
Point `TEST_REDIS_URL` at an instance to run it.

The store is built *inside* the coroutine because `redis.asyncio` binds its
connections to the running event loop.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from app.services.job_store import JOB_COMPLETED, JOB_QUEUED, RedisJobStore, new_job

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6399/0")


def run(scenario) -> None:
    async def main() -> None:
        store = RedisJobStore(REDIS_URL, ttl_seconds=60, queue_key=f"tts:test:{uuid.uuid4().hex}")
        try:
            reachable = await store.ping()
        except Exception:  # noqa: BLE001 - any connection failure means "skip"
            reachable = False
        if not reachable:
            pytest.skip(f"no redis at {REDIS_URL}")
        try:
            await scenario(store)
        finally:
            await store.close()

    asyncio.run(main())


def test_roundtrip_keeps_untouched_fields():
    job = new_job(text="xin chào", language="vi", fmt="mp3", user_id=7)

    async def scenario(store: RedisJobStore):
        await store.create(job)
        created = await store.get(job.id)
        assert created is not None
        assert created.status == JOB_QUEUED
        assert created.text == "xin chào"
        assert created.user_id == 7
        assert created.error is None
        assert created.audio_path is None

        # A late progress write must not clobber the terminal status: updates write
        # individual hash fields instead of read-modify-write.
        await store.update(job.id, status=JOB_COMPLETED, progress=100, finished_at=123.5)
        await store.update(job.id, progress=99, stage="late")
        got = await store.get(job.id)
        assert got is not None
        assert got.status == JOB_COMPLETED
        assert got.progress == 99
        assert got.finished_at == pytest.approx(123.5)
        assert got.text == "xin chào"

    run(scenario)


def test_public_payload_omits_text_and_path():
    job = new_job(text="secret-ish", language="vi", fmt="mp3")

    async def scenario(store: RedisJobStore):
        await store.create(job)
        await store.update(job.id, status=JOB_COMPLETED, audio_path="/data/output/x.mp3")
        got = await store.get(job.id)
        assert got is not None
        payload = got.public()
        assert payload["has_audio"] is True
        assert payload["text_length"] == len("secret-ish")
        assert "text" not in payload
        assert "audio_path" not in payload

    run(scenario)


def test_queue_is_fifo_and_empty_returns_none():
    first = new_job(text="a", language="vi", fmt="mp3")
    second = new_job(text="b", language="vi", fmt="mp3")

    async def scenario(store: RedisJobStore):
        await store.create(first)
        await store.create(second)
        await store.enqueue(first.id)
        await store.enqueue(second.id)
        assert await store.dequeue(timeout=1) == first.id
        assert await store.dequeue(timeout=1) == second.id
        assert await store.dequeue(timeout=1) is None

    run(scenario)


def test_updating_a_missing_job_returns_none():
    async def scenario(store: RedisJobStore):
        assert await store.update("does-not-exist", status=JOB_COMPLETED) is None

    run(scenario)
