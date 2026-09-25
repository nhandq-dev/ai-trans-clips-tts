"""TTS per segment — calls the TTS worker (plan/009 T2.1)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import json as _json
import os
import time
import uuid
from pathlib import Path

import httpx
from errors import TTS_FAILED, PermanentError, TransientError
from resilience import call_with_breaker_and_retry

TTS_WORKER_URL = os.getenv("TTS_WORKER_URL", "http://127.0.0.1:8004").rstrip("/")
HMAC_KEYS_JSON = os.getenv("HMAC_KEYS_JSON", "")
TTS_CONCURRENCY = int(os.getenv("TTS_CONCURRENCY", "2"))

# Dubbing runs on the same TTS worker as direct user requests, so it submits jobs
# with a priority far above any plan (users sit at 10..100). Otherwise a couple of
# long direct requests can starve a video's dubbing (plan/014 P3.1).
PIPELINE_TTS_PRIORITY = int(os.getenv("PIPELINE_TTS_PRIORITY", "500"))
TTS_ASYNC_JOBS = os.getenv("PIPELINE_TTS_ASYNC", "true").strip().lower() not in ("0", "false", "no")
TTS_JOB_TIMEOUT_SECONDS = float(os.getenv("PIPELINE_TTS_JOB_TIMEOUT_SECONDS", "900"))
TTS_JOB_POLL_SECONDS = float(os.getenv("PIPELINE_TTS_JOB_POLL_SECONDS", "0.5"))


class _JobsUnsupported(Exception):
    """The worker predates the async job endpoints; fall back to the blocking call."""


# pipeline signs TTS calls with its own key, which the TTS worker now trusts
# via the shared HMAC_KEYS_JSON. Prefer pipeline-key-1, fall back to the first key.


def _signing_key() -> tuple[str, str]:
    try:
        keys = _json.loads(HMAC_KEYS_JSON) if HMAC_KEYS_JSON else {}
    except Exception:
        keys = {}
    if not keys:
        return "", ""
    # prefer pipeline-key-1
    if "pipeline-key-1" in keys:
        return "pipeline-key-1", keys["pipeline-key-1"]
    # else first
    k, v = next(iter(keys.items()))
    return k, v


def _hmac_headers(method: str, path_and_query: str, body: bytes) -> dict[str, str]:
    key_id, secret = _signing_key()
    if not key_id:
        return {}
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    bh = hashlib.sha256(body).hexdigest()
    canon = "\n".join([method.upper(), path_and_query, ts, nonce, bh])
    sig = hmac.new(secret.encode(), canon.encode(), hashlib.sha256).hexdigest()
    return {
        "X-TTS-Key-Id": key_id,
        "X-TTS-Timestamp": ts,
        "X-TTS-Nonce": nonce,
        "X-TTS-Content-SHA256": bh,
        "X-TTS-Signature": sig,
        "X-Request-Id": uuid.uuid4().hex,
    }


async def _synthesize_sync(client: httpx.AsyncClient, body_dict: dict) -> bytes:
    body = json.dumps(body_dict).encode()
    headers = _hmac_headers("POST", "/v1/tts", body)
    headers["Content-Type"] = "application/json"
    resp = await client.post(f"{TTS_WORKER_URL}/v1/tts", content=body, headers=headers)
    if resp.status_code == 400:
        raise PermanentError(TTS_FAILED, f"TTS 400: {resp.text[:500]}")
    if resp.status_code == 429:
        raise TransientError(TTS_FAILED, f"TTS 429 rate limited: {resp.text[:200]}")
    if resp.status_code >= 500:
        raise TransientError(TTS_FAILED, f"TTS {resp.status_code}: {resp.text[:500]}")
    if resp.status_code != 200:
        raise TransientError(TTS_FAILED, f"TTS {resp.status_code}: {resp.text[:300]}")
    return resp.content


async def _synthesize_via_job(client: httpx.AsyncClient, body_dict: dict) -> bytes:
    """Queue a high-priority job, wait for it, then download the audio."""
    body = json.dumps({**body_dict, "priority": PIPELINE_TTS_PRIORITY}).encode()
    headers = _hmac_headers("POST", "/v1/tts/jobs", body)
    headers["Content-Type"] = "application/json"
    resp = await client.post(f"{TTS_WORKER_URL}/v1/tts/jobs", content=body, headers=headers)
    if resp.status_code in (404, 405):
        raise _JobsUnsupported()
    if resp.status_code == 400:
        raise PermanentError(TTS_FAILED, f"TTS job 400: {resp.text[:500]}")
    if resp.status_code >= 500:
        raise TransientError(TTS_FAILED, f"TTS job {resp.status_code}: {resp.text[:300]}")
    if resp.status_code not in (200, 202):
        raise TransientError(TTS_FAILED, f"TTS job {resp.status_code}: {resp.text[:300]}")

    job_id = resp.json().get("job_id")
    if not job_id:
        raise TransientError(TTS_FAILED, "TTS job response had no job_id")

    status_path = f"/v1/tts/jobs/{job_id}"
    audio_path = f"{status_path}/audio"
    deadline = time.monotonic() + TTS_JOB_TIMEOUT_SECONDS
    while True:
        if time.monotonic() > deadline:
            raise TransientError(TTS_FAILED, f"TTS job {job_id} timed out")
        await asyncio.sleep(TTS_JOB_POLL_SECONDS)

        poll = await client.get(
            f"{TTS_WORKER_URL}{status_path}", headers=_hmac_headers("GET", status_path, b"")
        )
        if poll.status_code in (404, 405):
            raise _JobsUnsupported()
        if poll.status_code >= 500:
            raise TransientError(TTS_FAILED, f"TTS job poll {poll.status_code}")
        if poll.status_code != 200:
            raise TransientError(TTS_FAILED, f"TTS job poll {poll.status_code}: {poll.text[:200]}")

        state = poll.json()
        status = state.get("status")
        if status == "failed":
            message = str(state.get("error") or "TTS job failed")
            if "exceeds the" in message or "not valid" in message:
                raise PermanentError(TTS_FAILED, message)
            raise TransientError(TTS_FAILED, message)
        if status != "completed":
            continue

        audio = await client.get(
            f"{TTS_WORKER_URL}{audio_path}", headers=_hmac_headers("GET", audio_path, b"")
        )
        if audio.status_code >= 500:
            raise TransientError(TTS_FAILED, f"TTS job audio {audio.status_code}")
        if audio.status_code != 200:
            raise TransientError(
                TTS_FAILED, f"TTS job audio {audio.status_code}: {audio.text[:200]}"
            )
        return audio.content


async def _synthesize(client: httpx.AsyncClient, body_dict: dict) -> bytes:
    """Prefer the async job path so dubbing outranks direct requests."""
    if TTS_ASYNC_JOBS:
        try:
            return await _synthesize_via_job(client, body_dict)
        except _JobsUnsupported:
            pass
    return await _synthesize_sync(client, body_dict)


async def _call_tts(text: str, language: str, voice: str | None, dest: Path) -> Path:
    """Single TTS call — no retry/breaker, just the HTTP."""
    # cache-aside (T1.5)
    try:
        from cache import get_cache, put_cache, tts_cache_key

        engine = "vieneu" if language == "vi" else "edge-tts"
        _key = tts_cache_key(text, language, voice, engine)
        _cached = get_cache(_key)
        if _cached:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(str(dest) + ".tmp")
            tmp.write_bytes(_cached)
            tmp.replace(dest)
            return dest
    except Exception:
        pass

    body_dict = {"text": text, "language": language}
    if voice:
        body_dict["voice"] = voice
    timeout = httpx.Timeout(60.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        content = await _synthesize(client, body_dict)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(dest) + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(dest)
    try:
        from cache import put_cache, tts_cache_key

        engine = "vieneu" if language == "vi" else "edge-tts"
        put_cache(tts_cache_key(text, language, voice, engine), content)
    except Exception:
        pass
    return dest


async def synthesize_segment(text: str, language: str, voice: str | None, dest: Path) -> Path:
    """Synthesize one segment with breaker+retry (plan/009 T2.5)."""
    return await call_with_breaker_and_retry("tts", _call_tts, text, language, voice, dest)


async def synthesize_all(
    segments: list,
    language: str,
    voice: str | None,
    out_dir: str | Path,
) -> list[Path]:
    """Synthesize all segments in parallel (concurrency 2-3). Returns list of mp3 paths.

    One segment failing does not kill the job (unless strict mode). Caller decides.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(TTS_CONCURRENCY)

    async def _one(idx: int, text: str) -> Path | None:
        async with sem:
            dest = out_dir / f"seg_{idx:04d}.mp3"
            if dest.exists() and dest.stat().st_size > 0:
                return dest
            try:
                return await synthesize_segment(text, language, voice, dest)
            except PermanentError:
                # invalid voice etc — don't retry, mark and continue
                # we still return None so caller can decide
                raise
            except TransientError:
                # breaker may have opened; propagate but caller can handle partial
                raise

    tasks = []
    for idx, seg in enumerate(segments):
        # seg may be Segment or str
        text = seg.target_text if hasattr(seg, "target_text") else str(seg)
        tasks.append(_one(idx, text))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    paths: list[Path] = []
    for r in results:
        if isinstance(r, Exception):
            # for MVP, raise first permanent, but allow partial
            # we raise so orchestrator can mark partial:7/20
            raise r
        if r is not None:
            paths.append(r)
    return paths
