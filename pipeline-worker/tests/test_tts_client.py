"""Pipeline TTS client: async job path, fallback, and error mapping (plan/014 P3.1)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from errors import PermanentError, TransientError  # noqa: E402
from tts import PIPELINE_TTS_PRIORITY, _synthesize, _synthesize_via_job  # noqa: E402


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_job_path_uses_high_priority_and_downloads_audio():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v1/tts/jobs":
            body = json.loads(request.content)
            assert body["priority"] == PIPELINE_TTS_PRIORITY
            assert body["text"] == "xin chào"
            return httpx.Response(202, json={"job_id": "j1", "status": "queued"})
        if request.url.path == "/v1/tts/jobs/j1":
            return httpx.Response(200, json={"status": "completed"})
        if request.url.path == "/v1/tts/jobs/j1/audio":
            return httpx.Response(200, content=b"mp3-bytes")
        return httpx.Response(404)

    async def scenario():
        async with _client(handler) as client:
            return await _synthesize_via_job(client, {"text": "xin chào", "language": "vi"})

    assert asyncio.run(scenario()) == b"mp3-bytes"
    assert seen == [
        "POST /v1/tts/jobs",
        "GET /v1/tts/jobs/j1",
        "GET /v1/tts/jobs/j1/audio",
    ]


def test_job_path_ignores_intermediate_progress():
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/tts/jobs":
            return httpx.Response(202, json={"job_id": "j2"})
        if request.url.path == "/v1/tts/jobs/j2":
            polls["n"] += 1
            if polls["n"] < 2:
                return httpx.Response(200, json={"status": "processing", "progress": 40})
            return httpx.Response(200, json={"status": "completed"})
        return httpx.Response(200, content=b"audio")

    async def scenario():
        async with _client(handler) as client:
            return await _synthesize_via_job(client, {"text": "hi", "language": "vi"})

    assert asyncio.run(scenario()) == b"audio"
    assert polls["n"] == 2


@pytest.mark.parametrize(
    "message,expected",
    [
        ("text exceeds the asynchronous limit of 30000", PermanentError),
        ("voice 'X' is not valid for language 'vi'", PermanentError),
        ("vieneu produced no audio for chunk 1/2", TransientError),
    ],
)
def test_job_failure_maps_to_the_right_error(message, expected):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/tts/jobs":
            return httpx.Response(202, json={"job_id": "j3"})
        if request.url.path == "/v1/tts/jobs/j3":
            return httpx.Response(200, json={"status": "failed", "error": message})
        return httpx.Response(404)

    async def scenario():
        async with _client(handler) as client:
            await _synthesize_via_job(client, {"text": "hi", "language": "vi"})

    with pytest.raises(expected):
        asyncio.run(scenario())


def test_falls_back_to_the_blocking_endpoint_when_jobs_are_missing():
    """An older worker without the job endpoints must keep working."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/tts/jobs":
            return httpx.Response(404)
        if request.url.path == "/v1/tts":
            return httpx.Response(200, content=b"sync-audio")
        return httpx.Response(404)

    async def scenario():
        async with _client(handler) as client:
            return await _synthesize(client, {"text": "hi", "language": "vi"})

    assert asyncio.run(scenario()) == b"sync-audio"
    assert paths == ["/v1/tts/jobs", "/v1/tts"]
