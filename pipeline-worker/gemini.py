"""Gemini client for transcription + translation (plan/009 T0.3, T1.2).

Uses ``google-genai`` SDK with structured output (response_schema). Supports
model fallback and tenacity retry for transient errors (429/503/timeout).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from errors import GEMINI_FAILED, GEMINI_NOT_CONFIGURED, PermanentError, TransientError
from google import genai
from google.genai import types
from schemas import TranscriptionResult
from tenacity import (
    retry_if_exception_type,
    stop_after_attempt,
    stop_after_delay,
    wait_exponential_jitter,
)

logger = logging.getLogger("pipeline-worker.gemini")

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.7-flash")
FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv(
        "GEMINI_FALLBACK_MODELS", "gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite"
    ).split(",")
    if m.strip()
]


def _client() -> genai.Client:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise PermanentError(GEMINI_NOT_CONFIGURED, "GEMINI_API_KEY is not configured")
    return genai.Client(api_key=api_key)


def _prompt(source_language: str, target_language: str) -> str:
    src = (
        f"Source language is '{source_language}'."
        if source_language != "auto"
        else "Detect the source language."
    )
    return (
        f"{src} Target language is '{target_language}'.\n"
        "Listen to the audio and return a JSON object with:\n"
        "- detected_language: the source language code (e.g. en, vi, zh)\n"
        "- segments: list of {start, end, source_text, target_text} where\n"
        "  start/end are seconds (float), source_text is verbatim transcript\n"
        "  in the source language, target_text is translation in target language.\n"
        "Rules: sort by start, drop empty, do not hallucinate. "
        "If silent/no speech, return detected_language='unknown' and segments=[]."
    )


def _classify_genai_error(exc: Exception) -> type[TransientError] | type[PermanentError]:
    """Map genai SDK errors to transient/permanent for tenacity."""
    msg = str(exc).lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    # 429 rate limit, 503 high demand, timeout, 500 are transient
    if status in (429, 500, 502, 503, 504) or any(
        s in msg
        for s in ("503", "429", "unavailable", "high demand", "rate limit", "timeout", "deadline")
    ):
        return TransientError
    # 400 bad request, 404 model not found for non-existent model name
    if status in (400, 404) and "not found" not in msg:
        return PermanentError
    # Model not found but we want fallback -> treat as transient for fallback loop
    if "not found" in msg:
        return TransientError
    return TransientError


async def _call_once(
    client: genai.Client,
    model: str,
    audio_bytes: bytes,
    prompt: str,
    mime_type: str = "audio/flac",
) -> TranscriptionResult:
    """Single model call with tenacity retry for transient blips on that model."""
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=TranscriptionResult,
        temperature=0.2,
    )
    # google-genai is sync; run in thread so we don't block the event loop
    import asyncio

    def _do() -> TranscriptionResult:
        # Retry transient errors on this model with backoff
        # We use a sync tenacity loop here (not async) because the SDK is sync.
        from tenacity import Retrying

        for attempt in Retrying(
            wait=wait_exponential_jitter(initial=1, max=10, jitter=3),
            stop=(stop_after_attempt(3) | stop_after_delay(30)),
            retry=retry_if_exception_type(TransientError),
            reraise=True,
        ):
            with attempt:
                try:
                    resp = client.models.generate_content(
                        model=model,
                        contents=[
                            types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                            prompt,
                        ],
                        config=config,
                    )
                except Exception as exc:
                    err_cls = _classify_genai_error(exc)
                    raise err_cls(GEMINI_FAILED, str(exc)) from exc

                # SDK returns .parsed when response_schema is set
                if hasattr(resp, "parsed") and resp.parsed is not None:
                    parsed = resp.parsed
                    if isinstance(parsed, TranscriptionResult):
                        return parsed
                    return TranscriptionResult.model_validate(parsed)
                # Fallback: parse text
                import json

                try:
                    data = json.loads(resp.text)  # type: ignore[union-attr]
                    return TranscriptionResult.model_validate(data)
                except Exception as exc:
                    raise TransientError(
                        GEMINI_FAILED, f"invalid json from {model}: {exc}"
                    ) from exc
        raise TransientError(GEMINI_FAILED, f"exhausted retries for {model}")

    return await asyncio.to_thread(_do)


async def transcribe_and_translate(
    audio_path: str | Path,
    source_language: str = "auto",
    target_language: str = "vi",
) -> TranscriptionResult:
    """Transcribe + translate a single audio chunk (FLAC 16k mono).

    Tries ``GEMINI_MODEL`` then ``GEMINI_FALLBACK_MODELS`` in order. Raises
    ``GEMINI_FAILED`` if all models fail.
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise PermanentError(GEMINI_FAILED, f"audio not found: {audio_path}")
    audio_bytes = audio_path.read_bytes()
    if not audio_bytes:
        raise PermanentError(GEMINI_FAILED, "empty audio file")

    # cache-aside (T1.5): check Gemini cache before calling
    try:
        from cache import gemini_cache_key, get_cache, put_cache

        for _model in [DEFAULT_MODEL] + [m for m in FALLBACK_MODELS if m != DEFAULT_MODEL]:
            _key = gemini_cache_key(audio_bytes, source_language, target_language, _model)
            _cached = get_cache(_key)
            if _cached:
                try:
                    _res = TranscriptionResult.model_validate_json(_cached)
                    logger.info("gemini cache hit model=%s", _model)
                    return _res
                except Exception:
                    pass
    except Exception:
        pass

    client = _client()
    prompt = _prompt(source_language, target_language)
    models = [DEFAULT_MODEL] + [m for m in FALLBACK_MODELS if m != DEFAULT_MODEL]

    last_exc: Exception | None = None
    for model in models:
        try:
            logger.info(
                "gemini call model=%s source=%s target=%s", model, source_language, target_language
            )
            result = await _call_once(client, model, audio_bytes, prompt)
            # Post-processing: sort, drop empty, merge <0.4s (plan/009 T1.2)
            from segments import normalize_segments

            result.segments = normalize_segments(result.segments)
            logger.info("gemini success model=%s segments=%d", model, len(result.segments))
            # put cache
            try:
                from cache import gemini_cache_key, put_cache

                put_cache(
                    gemini_cache_key(audio_bytes, source_language, target_language, model),
                    result.model_dump_json().encode(),
                )
            except Exception:
                pass
            return result
        except PermanentError as exc:
            logger.warning("gemini permanent error model=%s: %s", model, exc.message)
            last_exc = exc
            break
        except TransientError as exc:
            logger.warning(
                "gemini transient error model=%s: %s — trying fallback", model, exc.message
            )
            last_exc = exc
            continue
        except Exception as exc:
            logger.warning("gemini unknown error model=%s: %s", model, exc)
            last_exc = TransientError(GEMINI_FAILED, str(exc))
            continue

    raise TransientError(GEMINI_FAILED, f"all gemini models failed: {last_exc}")


# Sync wrapper for tests / scripts
def transcribe_and_translate_sync(*args, **kwargs) -> TranscriptionResult:
    import asyncio

    return asyncio.run(transcribe_and_translate(*args, **kwargs))
