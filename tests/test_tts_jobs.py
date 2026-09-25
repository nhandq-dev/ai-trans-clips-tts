"""Async TTS job path (plan/014 Phase 1)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.services.job_store import (
    JOB_COMPLETED,
    JOB_FAILED,
    JOB_QUEUED,
    MemoryJobStore,
    get_job_store,
    new_job,
    reset_job_store,
)


@pytest.fixture(autouse=True)
def _fresh_store():
    reset_job_store()
    yield
    reset_job_store()


def _post_job(client, signer, payload: dict):
    body = json.dumps(payload).encode()
    headers = signer("POST", "/v1/tts/jobs", body)
    headers["Content-Type"] = "application/json"
    return client.post("/v1/tts/jobs", content=body, headers=headers)


def _get(client, signer, path: str):
    return client.get(path, headers=signer("GET", path))


# --- store --------------------------------------------------------------------


def test_memory_store_roundtrip_and_public_hides_the_text():
    async def scenario():
        store = MemoryJobStore()
        job = new_job(text="xin chào", language="vi", fmt="mp3")
        await store.create(job)
        await store.enqueue(job.id)

        assert await store.dequeue(timeout=1) == job.id
        assert await store.dequeue(timeout=0.01) is None

        updated = await store.update(
            job.id, status=JOB_COMPLETED, progress=100, audio_path="/tmp/a"
        )
        assert updated is not None
        assert updated.status == JOB_COMPLETED

        public = (await store.get(job.id)).public()
        assert public["has_audio"] is True
        assert public["text_length"] == len("xin chào")
        assert "text" not in public
        assert "audio_path" not in public

    asyncio.run(scenario())


def test_memory_store_update_of_missing_job_is_a_noop():
    async def scenario():
        store = MemoryJobStore()
        assert await store.update("nope", status=JOB_COMPLETED) is None

    asyncio.run(scenario())


# --- runner -------------------------------------------------------------------


def _fake_synthesis(monkeypatch, *, progress=(40, "half"), fail: Exception | None = None):
    """Replace `generate_tts` inside the runner; returns the path it wrote."""
    from app.services import job_runner

    written = {}

    def fake(text, language, voice, out_path, fmt, on_progress, voice_data):  # noqa: ANN001
        if on_progress:
            on_progress(*progress)
        if fail is not None:
            raise fail
        Path(out_path).write_bytes(b"audio-bytes")
        written["path"] = str(out_path)
        return str(out_path)

    monkeypatch.setattr(job_runner, "generate_tts", fake)
    return written


def test_process_job_completes_and_mirrors_progress(monkeypatch):
    written = _fake_synthesis(monkeypatch)

    async def scenario():
        from app.services.job_runner import process_job

        store = MemoryJobStore()
        job = new_job(text="xin chào", language="vi", fmt="mp3")
        await store.create(job)

        await process_job(store, job.id)
        # progress is written through run_coroutine_threadsafe from the worker
        # thread; give the loop a tick to drain it.
        await asyncio.sleep(0)

        done = await store.get(job.id)
        assert done is not None
        assert done.status == JOB_COMPLETED
        assert done.progress == 100
        assert done.audio_path == written["path"]
        assert done.finished_at is not None

    asyncio.run(scenario())


def test_process_job_records_failure(monkeypatch):
    _fake_synthesis(monkeypatch, fail=RuntimeError("edge-tts blocked (403)"))

    async def scenario():
        from app.services.job_runner import process_job

        store = MemoryJobStore()
        job = new_job(text="hello", language="en", fmt="mp3")
        await store.create(job)

        await process_job(store, job.id)

        failed = await store.get(job.id)
        assert failed is not None
        assert failed.status == JOB_FAILED
        assert "403" in (failed.error or "")

    asyncio.run(scenario())


def test_process_job_skips_paths_that_are_not_queued(monkeypatch):
    calls = _fake_synthesis(monkeypatch)

    async def scenario():
        from app.services.job_runner import process_job

        store = MemoryJobStore()
        job = new_job(text="hi", language="vi", fmt="mp3", status=JOB_COMPLETED)
        await store.create(job)

        await process_job(store, job.id)
        assert calls == {}

    asyncio.run(scenario())


# --- routes -------------------------------------------------------------------


def test_create_job_returns_202_and_is_queryable(client, signer):
    response = _post_job(client, signer, {"text": "xin chào", "language": "vi", "voice": "Adam"})
    assert response.status_code == 202

    payload = response.json()
    assert payload["status"] == JOB_QUEUED
    assert payload["text_length"] == len("xin chào")
    assert payload["has_audio"] is False
    assert "text" not in payload

    fetched = _get(client, signer, f"/v1/tts/jobs/{payload['job_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["job_id"] == payload["job_id"]


def test_unknown_job_returns_404(client, signer):
    assert _get(client, signer, "/v1/tts/jobs/deadbeef").status_code == 404
    assert _get(client, signer, "/v1/tts/jobs/deadbeef/audio").status_code == 404


def test_audio_is_409_until_the_job_completes(client, signer):
    job_id = _post_job(client, signer, {"text": "xin chào", "language": "vi"}).json()["job_id"]
    response = _get(client, signer, f"/v1/tts/jobs/{job_id}/audio")
    assert response.status_code == 409
    assert "queued" in response.json()["error"]


def test_audio_is_served_once_completed(client, signer):
    job_id = _post_job(client, signer, {"text": "xin chào", "language": "vi"}).json()["job_id"]
    store = get_job_store()

    async def complete():
        await store.update(
            job_id,
            status=JOB_COMPLETED,
            progress=100,
            audio_path=str(Path(__file__).with_name("conftest.py")),
            finished_at=1.0,
        )

    asyncio.run(complete())
    response = _get(client, signer, f"/v1/tts/jobs/{job_id}/audio")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert response.content


def test_events_stream_reports_terminal_status(client, signer):
    job_id = _post_job(client, signer, {"text": "xin chào", "language": "vi"}).json()["job_id"]
    store = get_job_store()

    async def fail():
        await store.update(job_id, status=JOB_FAILED, error="boom", finished_at=1.0)

    asyncio.run(fail())
    path = f"/v1/tts/jobs/{job_id}/events"
    with client.stream("GET", path, headers=signer("GET", path)) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())
    assert "data:" in body
    assert JOB_FAILED in body


def test_async_jobs_require_redis_when_running_multiple_workers(client, signer, monkeypatch):
    from app.core.config import get_settings

    reset_job_store()
    monkeypatch.setattr(get_settings(), "tts_workers", 2)

    response = _post_job(client, signer, {"text": "xin chào", "language": "vi"})
    assert response.status_code == 503
    assert "REDIS_URL" in response.json()["error"]


def test_voices_reports_both_ceilings(client, signer):
    """The catalog is the contract the API clamps plan caps against."""
    response = client.get("/v1/voices", headers=signer("GET", "/v1/voices"))
    assert response.status_code == 200
    limits = response.json()["limits"]
    assert limits["syncMaxTextLength"] == 2000
    assert limits["asyncMaxTextLength"] == 30000


def test_job_is_invisible_to_another_user(client, signer):
    payload = _post_job(client, signer, {"text": "xin chào", "language": "vi", "user_id": 7}).json()

    mine = _get(client, signer, f"/v1/tts/jobs/{payload['job_id']}?user_id=7")
    assert mine.status_code == 200

    theirs = _get(client, signer, f"/v1/tts/jobs/{payload['job_id']}?user_id=8")
    assert theirs.status_code == 404
    assert (
        _get(client, signer, f"/v1/tts/jobs/{payload['job_id']}/audio?user_id=8").status_code == 404
    )
