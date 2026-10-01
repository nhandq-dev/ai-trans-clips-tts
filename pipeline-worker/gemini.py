"""Gemini client for transcription + translation (plan/009 T0.3, T1.2).

Uses ``google-genai`` SDK with structured output (response_schema). Supports
model fallback and tenacity retry for transient errors (429/503/timeout).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from errors import (
    GEMINI_FAILED,
    GEMINI_NOT_CONFIGURED,
    PermanentError,
    QuotaExhaustedError,
    TransientError,
)
from google import genai
from google.genai import types
from schemas import Segment, TranscriptionResult
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


def _split_keys(value: str) -> list[str]:
    return [key.strip() for key in (value or "").split(",") if key.strip()]


def key_pool(tier: str | None = None) -> list[str]:
    """API keys for a billing tier (plan/015).

    Free plans share a pool of free-tier keys; paid plans use the paid key(s).
    Both fall back to the legacy single `GEMINI_API_KEY`, so a deployment keeps
    working before the pools are filled in.
    """
    if tier == "free":
        keys = _split_keys(os.getenv("GEMINI_API_KEYS_FREE", ""))
    elif tier == "paid":
        keys = _split_keys(os.getenv("GEMINI_API_KEY_PAID", ""))
    else:
        keys = []
    return keys or _split_keys(os.getenv("GEMINI_API_KEY", ""))


def _client(api_key: str) -> genai.Client:
    if not api_key:
        raise PermanentError(GEMINI_NOT_CONFIGURED, "no Gemini API key is configured")
    return genai.Client(api_key=api_key)


def _prompt(target_language: str, context: str = "", glossary: str = "") -> str:
    return (
        f"Target language: {target_language}.\n"
        f"Topic/tone: {context or 'general'}. Glossary: {glossary or 'none'}.\n"
        "Transcribe the audio and translate it.\n"
        'Output JSON only: {"lang":"<ISO code or unknown>",'
        '"s":[[start,end,"original","translation"],...]}\n'
        "Timing:\n"
        '- start/end are strings "MM:SS.d" relative to the start of this clip.\n'
        "- start = first word begins; end = last word ends. Exclude silence, music, pauses.\n"
        "- Sorted by start. No overlap (end <= next start). Gaps are allowed.\n"
        "- One sentence/phrase per segment, 1-7 seconds; split long sentences at natural pauses.\n"
        "Content:\n"
        '- "original": verbatim speech in the source language. Only audible speech; '
        'never invent text; use "[?]" if unclear.\n'
        f'- "translation": natural {target_language}, max ~42 chars/line, follow the glossary.\n'
        'No speech: {"lang":"unknown","s":[]}'
    )


def _to_sec(ts: str) -> float:
    """Convert MM:SS.d or HH:MM:SS.d to seconds."""
    parts = ts.split(":")
    if len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    raise ValueError(f"invalid time format: {ts}")


#: Exhausted quota is not worth retrying: a per-day/per-project limit will not
#: recover within the job's lifetime, and retrying burns the remaining budget of
#: the fallback models too.
_QUOTA_MARKERS = ("quota", "resource_exhausted", "billing", "exceeded your current")


def _classify_genai_error(exc: Exception) -> type[Exception]:
    """Map genai SDK errors to transient/permanent for tenacity.

    Permanent errors fail immediately with a precise code; only genuinely
    retryable conditions (per-minute rate limits, 5xx, timeouts) are retried.
    """
    msg = str(exc).lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)

    # Quota / billing is per model: skip its retries but still try the next model.
    if any(marker in msg for marker in _QUOTA_MARKERS):
        return QuotaExhaustedError
    # Rate limit, high demand, timeout, 5xx are transient.
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
) -> tuple[str, list[Segment]]:
    """Single model call with tenacity retry for transient blips on that model."""
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        # We don't use response_schema here; we parse JSON manually for the new format
        temperature=0.2,
    )
    # google-genai is sync; run in thread so we don't block the event loop
    import asyncio

    def _do() -> tuple[str, list[Segment]]:
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
                            types.Part.from_bytes(data=audio_bytes, mime_type="audio/flac"),
                            prompt,
                        ],
                        config=config,
                    )
                except Exception as exc:
                    err_cls = _classify_genai_error(exc)
                    raise err_cls(GEMINI_FAILED, str(exc)) from exc

                usage = getattr(resp, "usage_metadata", None)
                _ = int(getattr(usage, "prompt_token_count", 0) or 0)
                _ = int(getattr(usage, "candidates_token_count", 0) or 0)

                # Parse JSON response (new format)
                text = resp.text  # type: ignore[union-attr]
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise TransientError(
                        GEMINI_FAILED, f"invalid json from {model}: {exc}"
                    ) from exc

                segs_raw = data.get("s", [])
                segments: list[Segment] = []
                for item in segs_raw:
                    if not isinstance(item, (list, tuple)) or len(item) < 4:
                        continue
                    start_ts, end_ts, original, translation = item[:4]
                    if not original.strip() or not translation.strip():
                        continue
                    try:
                        start = _to_sec(start_ts)
                        end = _to_sec(end_ts)
                    except ValueError:
                        continue
                    if end <= start:
                        continue
                    segments.append(
                        Segment(
                            start=start,
                            end=end,
                            source_text=original.strip(),
                            target_text=translation.strip(),
                        )
                    )

                return data.get("lang", "unknown"), segments
        raise TransientError(GEMINI_FAILED, f"exhausted retries for {model}")

    return await asyncio.to_thread(_do)


async def transcribe_and_translate(
    audio_path: str | Path,
    target_language: str = "vi",
    context: str = "",
    glossary: str = "",
    tier: str | None = None,
) -> TranscriptionResult:
    """Transcribe + translate a single audio chunk (FLAC 16k mono).

    Tries ``GEMINI_MODEL`` then ``GEMINI_FALLBACK_MODELS`` in order and, within each
    model, every key of the caller's tier — quotas are per key *and* model, so a free
    pool multiplies the usable free quota (plan/015). Raises ``GEMINI_FAILED`` if
    every attempt fails.
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
            _key = gemini_cache_key(audio_bytes, "auto", target_language, _model)
            _cached = get_cache(_key)
            if _cached:
                try:
                    _res = TranscriptionResult.model_validate_json(_cached)
                    _res.input_tokens = 0
                    _res.output_tokens = 0
                    logger.info("gemini cache hit model=%s", _model)
                    return _res
                except Exception:
                    pass
    except Exception:
        pass

    keys = key_pool(tier)
    if not keys:
        raise PermanentError(GEMINI_NOT_CONFIGURED, "GEMINI_API_KEY is not configured")

    models = [DEFAULT_MODEL] + [m for m in FALLBACK_MODELS if m != DEFAULT_MODEL]
    attempts = [(model, key) for model in models for key in keys]

    last_exc: Exception | None = None
    for model, api_key in attempts:
        try:
            logger.info(
                "gemini call model=%s key=%s target=%s",
                model,
                api_key[-4:],
                target_language,
            )
            prompt = _prompt(target_language)
            lang, segments = await _call_once(_client(api_key), model, audio_bytes, prompt)

            # Post-processing: sort, drop empty, merge <0.4s (plan/009 T1.2)
            from segments import normalize_segments

            result = TranscriptionResult(
                detected_language=lang,
                segments=normalize_segments(segments),
            )
            logger.info("gemini success model=%s segments=%d", model, len(result.segments))
            # put cache
            try:
                from cache import gemini_cache_key, put_cache

                put_cache(
                    gemini_cache_key(audio_bytes, "auto", target_language, model),
                    result.model_dump_json().encode(),
                )
            except Exception:
                pass
            return result
        except QuotaExhaustedError as exc:
            logger.warning(
                "gemini quota exhausted model=%s key=%s: %s -- trying next",
                model,
                api_key[-4:],
                exc.message,
            )
            last_exc = exc
            continue
        except PermanentError as exc:
            logger.warning(
                "gemini permanent error model=%s key=%s: %s", model, api_key[-4:], exc.message
            )
            last_exc = exc
            continue
        except TransientError as exc:
            logger.warning(
                "gemini transient error model=%s: %s -- trying fallback", model, exc.message
            )
            last_exc = exc
            continue
        except Exception as exc:
            logger.warning("gemini unknown error model=%s: %s", model, exc)
            last_exc = TransientError(GEMINI_FAILED, str(exc))
            continue

    # Preserve the cause's class: a permanent failure (quota/billing/invalid
    # request) must not be reported as retryable, or the API would keep the job
    # alive and the UI would sit in `transcribing` with no explanation.
    detail = getattr(last_exc, "message", None) or str(last_exc)
    if isinstance(last_exc, QuotaExhaustedError):
        raise QuotaExhaustedError(
            GEMINI_FAILED, f"every gemini model is out of quota: {detail}"
        ) from last_exc
    if isinstance(last_exc, PermanentError):
        raise PermanentError(GEMINI_FAILED, f"gemini failed: {detail}") from last_exc
    raise TransientError(GEMINI_FAILED, f"all gemini models failed: {detail}") from last_exc


# Sync wrapper for tests / scripts
def transcribe_and_translate_sync(*args, **kwargs) -> TranscriptionResult:
    import asyncio

    return asyncio.run(transcribe_and_translate(*args, **kwargs))