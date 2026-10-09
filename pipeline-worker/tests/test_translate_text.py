"""Text-only Gemini translation (whisper-asr pipeline).

`translate_text` must: mutate segments in-place, keep batch order intact, map
returned lines by index, and pass a rolling context window. Errors must fall
through the model loop to the next model/key like the audio path does.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gemini  # noqa: E402
from schemas import Segment  # noqa: E402


def _segs(*pairs):
    return [
        Segment(start=i, end=float(i + 1), source_text=src, target_text="")
        for i, (src, _tgt) in enumerate(pairs)
    ]


def test_translate_noop_empty():
    asyncio.run(gemini.translate_text([], "zh", "vi"))
    assert True


def test_translate_fills_target_in_order(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    calls = []

    async def fake_call_text_once(client, model, prompt):  # noqa: ARG001
        calls.append((model, prompt))
        import json as _j

        body = prompt.rsplit("\n", 1)[-1]
        srcs = _j.loads(body)
        # echo: translation = "vi:" + source (trivial but order-preserving)
        return _j.dumps({"t": [f"VI({s})" for s in srcs]})

    monkeypatch.setattr(gemini, "_call_text_once", fake_call_text_once)
    monkeypatch.setattr(gemini, "_client", lambda api_key: object())
    monkeypatch.setattr(gemini, "DEFAULT_MODEL", "m1")
    monkeypatch.setattr(gemini, "FALLBACK_MODELS", [])

    segs = _segs(("你好", ""), ("世界", ""), ("好吗", ""))
    asyncio.run(gemini.translate_text(segs, "zh", "vi"))
    assert [s.target_text for s in segs] == ["VI(你好)", "VI(世界)", "VI(好吗)"]


def test_translate_passes_prev_context(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    captured = {}

    async def fake_call_text_once(client, model, prompt):  # noqa: ARG001
        captured["prompt"] = prompt
        import json as _j

        srcs = _j.loads(prompt.rsplit("\n", 1)[-1])
        return _j.dumps({"t": [f"VI({s})" for s in srcs]})

    monkeypatch.setattr(gemini, "_call_text_once", fake_call_text_once)
    monkeypatch.setattr(gemini, "_client", lambda api_key: object())
    monkeypatch.setattr(gemini, "DEFAULT_MODEL", "m1")
    monkeypatch.setattr(gemini, "FALLBACK_MODELS", [])

    segs = _segs(("hi", ""))
    asyncio.run(
        gemini.translate_text(
            segs,
            "zh",
            "vi",
            context="stones",
            glossary="石敢当=đá",
            prev_context="早 → sớm",
        )
    )
    prompt = captured["prompt"]
    assert "Context so far" in prompt
    assert "早 → sớm" in prompt
    assert "石敢当=đá" in prompt


def test_translate_falls_through_to_next_model(monkeypatch):
    """Quota on model 1 must move straight to model 2 (each has its own quota)."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(gemini, "DEFAULT_MODEL", "m1")
    monkeypatch.setattr(gemini, "FALLBACK_MODELS", ["m2"])
    monkeypatch.setattr(gemini, "_client", lambda api_key: object())

    calls = []

    async def fake_call_text_once(client, model, prompt):  # noqa: ARG001
        calls.append(model)
        if model == "m1":
            from errors import QuotaExhaustedError

            raise QuotaExhaustedError(gemini.GEMINI_FAILED, "m1 out of quota")
        import json as _j

        srcs = _j.loads(prompt.rsplit("\n", 1)[-1])
        return _j.dumps({"t": [f"VI({s})" for s in srcs]})

    monkeypatch.setattr(gemini, "_call_text_once", fake_call_text_once)

    segs = _segs(("hello", ""))
    asyncio.run(gemini.translate_text(segs, "en", "vi"))
    assert calls == ["m1", "m2"]
    assert segs[0].target_text == "VI(hello)"


def test_translate_missing_list_retries_fallback(monkeypatch):
    """Non-list response is transient; next model must succeed."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(gemini, "DEFAULT_MODEL", "m1")
    monkeypatch.setattr(gemini, "FALLBACK_MODELS", ["m2"])
    monkeypatch.setattr(gemini, "_client", lambda api_key: object())

    calls = []

    async def fake_call_text_once(client, model, prompt):  # noqa: ARG001
        calls.append(model)
        if model == "m1":
            from errors import TransientError

            raise TransientError(gemini.GEMINI_FAILED, "no list in response")
        import json as _j

        srcs = _j.loads(prompt.rsplit("\n", 1)[-1])
        return _j.dumps({"t": [f"VI({s})" for s in srcs]})

    monkeypatch.setattr(gemini, "_call_text_once", fake_call_text_once)

    segs = _segs(("a", ""))
    asyncio.run(gemini.translate_text(segs, "en", "vi"))
    assert calls == ["m1", "m2"]
    assert segs[0].target_text == "VI(a)"


def test_translate_no_config_raises(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEYS_FREE", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_PAID", raising=False)
    from errors import PermanentError

    with pytest.raises(PermanentError):
        asyncio.run(gemini.translate_text(_segs(("hello", "")), "zh", "vi"))
