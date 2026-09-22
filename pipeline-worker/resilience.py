"""Resilience helpers — tenacity retry + pybreaker circuit breaker (plan/009 T2.5).

Retry is inside, breaker outside so one exhausted retry counts as one breaker failure.

Usage:
    from resilience import get_breaker, retry_config

    breaker = get_breaker("tts")
    @retry_config("tts")
    async def call_tts(...): ...

    # or
    result = await breaker.call_async(retry_wrapped_fn)  # via helper
"""

from __future__ import annotations

import asyncio
import os

import pybreaker
from errors import PermanentError, TransientError
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, stop_after_delay, wait_exponential_jitter

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
MAX_ELAPSED = int(os.getenv("MAX_ELAPSED_SECONDS", "60"))
CIRCUIT_FAIL_MAX = int(os.getenv("CIRCUIT_BREAKER_FAIL_MAX", "5"))
CIRCUIT_RESET_TIMEOUT = int(os.getenv("CIRCUIT_BREAKER_RESET_TIMEOUT", "60"))

# global breaker registry per dependency
_breakers: dict[str, pybreaker.CircuitBreaker] = {}


def get_breaker(name: str) -> pybreaker.CircuitBreaker:
    if name not in _breakers:
        _breakers[name] = pybreaker.CircuitBreaker(
            fail_max=CIRCUIT_FAIL_MAX,
            reset_timeout=CIRCUIT_RESET_TIMEOUT,
            name=name,
            # pybreaker is sync; we call it via to_thread
        )
    return _breakers[name]


def retry_config(name: str | None = None) -> dict:
    """Return tenacity kwargs for ``AsyncRetrying``. Only retries TransientError."""
    return dict(
        wait=wait_exponential_jitter(initial=1, max=10, jitter=3),
        stop=(stop_after_attempt(MAX_RETRIES) | stop_after_delay(MAX_ELAPSED)),
        retry=retry_if_exception_type(TransientError),
        reraise=True,
    )


async def call_with_breaker_and_retry(
    breaker_name: str,
    func,
    *args,
    **kwargs,
):
    """Call ``func(*args, **kwargs)`` with retry inside, breaker outside.

    ``func`` may be sync or async. The breaker counts one failure per exhausted retry,
    not per attempt. We run the sync ``breaker.call`` in a thread so the event loop
    is not blocked.
    """
    breaker = get_breaker(breaker_name)
    cfg = retry_config(breaker_name)

    async def _retry_wrapped():
        async for attempt in AsyncRetrying(**cfg):
            with attempt:
                result = func(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    result = await result
                return result

    def _sync_wrapper():
        # run the async retry loop inside this thread's event loop
        return asyncio.run(_retry_wrapped())

    try:
        return await asyncio.to_thread(breaker.call, _sync_wrapper)
    except pybreaker.CircuitBreakerError as exc:
        raise TransientError("CIRCUIT_OPEN", f"breaker {breaker_name} open: {exc}") from exc
    except PermanentError:
        raise
    except TransientError:
        raise
    except Exception as exc:
        # unexpected -> treat as transient so breaker can trip
        raise TransientError("FAILED", str(exc)) from exc
