"""Gemini key pools and tier routing (plan/015).

Free plans share a pool of free-tier keys; paid plans use their own. Quotas are
per key *and* model, so within a model every key is tried before falling back to
the next model.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gemini  # noqa: E402
from errors import QuotaExhaustedError  # noqa: E402
from schemas import TranscriptionResult  # noqa: E402


class _Usage:
    prompt_token_count = 120
    candidates_token_count = 40


class _Response:
    def __init__(self) -> None:
        self.parsed = TranscriptionResult(detected_language="en", segments=[])
        self.usage_metadata = _Usage()


def test_free_pool_reads_its_own_list(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEYS_FREE", "f1, f2 ,,f3")
    monkeypatch.setenv("GEMINI_API_KEY_PAID", "p1")
    assert gemini.key_pool("free") == ["f1", "f2", "f3"]
    assert gemini.key_pool("paid") == ["p1"]


def test_pool_falls_back_to_the_legacy_single_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEYS_FREE", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_PAID", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "legacy")
    assert gemini.key_pool("free") == ["legacy"]
    assert gemini.key_pool("paid") == ["legacy"]
    assert gemini.key_pool(None) == ["legacy"]


def test_missing_keys_are_reported_not_silently_empty(monkeypatch):
    for name in ("GEMINI_API_KEYS_FREE", "GEMINI_API_KEY_PAID", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    async def scenario():
        return await gemini.transcribe_and_translate(_audio(), "en", "vi", tier="free")

    with pytest.raises(Exception) as excinfo:
        asyncio.run(scenario())
    assert "GEMINI_API_KEY" in str(excinfo.value)


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """The real cache dir is shared between runs; a stale entry would zero the tokens."""
    import cache as cache_module

    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path / "cache")


def _audio() -> Path:
    """Unique bytes per call so one test cannot serve another's cached result."""
    import tempfile
    import uuid

    handle = tempfile.NamedTemporaryFile(suffix=".flac", delete=False)
    handle.write(f"fake-audio-{uuid.uuid4().hex}".encode())
    handle.close()
    return Path(handle.name)


def test_free_keys_are_tried_before_the_next_model(monkeypatch):
    """Quota is per key and model, so all free keys are burned on model 1 first."""
    monkeypatch.setenv("GEMINI_API_KEYS_FREE", "f1,f2")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(gemini, "DEFAULT_MODEL", "m1")
    monkeypatch.setattr(gemini, "FALLBACK_MODELS", ["m2"])

    calls: list[tuple[str, str]] = []

    async def fake_call_once(client, model, audio_bytes, prompt, mime_type="audio/flac"):  # noqa: ANN001
        calls.append((model, str(client)))
        if model == "m1":
            raise QuotaExhaustedError("GEMINI_FAILED", f"{model} out of quota")
        return TranscriptionResult(detected_language="en", segments=[])

    monkeypatch.setattr(gemini, "_client", lambda api_key: api_key)
    monkeypatch.setattr(gemini, "_call_once", fake_call_once)

    result = asyncio.run(gemini.transcribe_and_translate(_audio(), "en", "vi", tier="free"))

    assert calls == [("m1", "f1"), ("m1", "f2"), ("m2", "f1")]
    assert result.detected_language == "en"


def test_usage_metadata_is_attached_to_the_result(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k1")
    monkeypatch.setattr(gemini, "DEFAULT_MODEL", "m1")
    monkeypatch.setattr(gemini, "FALLBACK_MODELS", [])

    class _Client:
        class models:  # noqa: N801 - mirroring the SDK shape
            @staticmethod
            def generate_content(**_kwargs):
                return _Response()

    monkeypatch.setattr(gemini, "_client", lambda api_key: _Client())

    result = asyncio.run(gemini.transcribe_and_translate(_audio(), "en", "vi"))
    assert (result.input_tokens, result.output_tokens) == (120, 40)
    # Tokens must not leak into the cached/Gemini-facing JSON.
    assert "input_tokens" not in json.loads(result.model_dump_json())
