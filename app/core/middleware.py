from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.config import Settings

logger = logging.getLogger("tts-worker.access")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, emit an access log line, and echo the id on the response."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        header = self._settings.request_id_header
        request_id = request.headers.get(header) or uuid.uuid4().hex
        request.state.request_id = request_id

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            self._log(request, status_code=None, duration_ms=self._elapsed_ms(start))
            raise

        response.headers[header] = request_id
        self._log(request, status_code=response.status_code, duration_ms=self._elapsed_ms(start))
        return response

    @staticmethod
    def _elapsed_ms(start: float) -> float:
        return round((time.perf_counter() - start) * 1000, 2)

    def _log(self, request: Request, status_code: int | None, duration_ms: float) -> None:
        if not self._settings.access_log_enabled:
            return
        logger.info(
            "request",
            extra={
                "request_id": getattr(request.state, "request_id", None),
                "method": request.method,
                "path": request.url.path,
                "status_code": status_code,
                "duration_ms": duration_ms,
                "engine": getattr(request.state, "engine", None),
                "language": getattr(request.state, "language", None),
                "text_length": getattr(request.state, "text_length", None),
            },
        )
