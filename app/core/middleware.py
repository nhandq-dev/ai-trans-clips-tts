from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.security import SignatureError, SignatureVerifier

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


class HMACAuthMiddleware:
    """Verify HMAC-signed requests before they reach the routes.

    `/health/*` stays unauthenticated so liveness/readiness probes need no signature. The request
    body is buffered once, verified, and then replayed to the downstream application.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self._app = app
        self._verifier = SignatureVerifier(settings)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path", "").startswith("/health"):
            await self._app(scope, receive, send)
            return

        body = await self._read_body(receive)

        try:
            self._verifier.verify(
                method=scope.get("method", ""),
                path_and_query=self._path_and_query(scope),
                headers=Headers(scope=scope),
                body=body,
            )
        except SignatureError as exc:
            await self._unauthorized(scope, receive, send, str(exc))
            return

        await self._app(scope, self._replay(body, receive), send)

    @staticmethod
    async def _read_body(receive: Receive) -> bytes:
        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.request":
                body += message.get("body", b"")
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break
        return body

    @staticmethod
    def _path_and_query(scope: Scope) -> str:
        path = scope.get("path", "")
        query = scope.get("query_string", b"").decode("latin-1")
        return f"{path}?{query}" if query else path

    @staticmethod
    def _replay(body: bytes, receive: Receive) -> Receive:
        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return replay_receive

    @staticmethod
    async def _unauthorized(scope: Scope, receive: Receive, send: Send, reason: str) -> None:
        state = scope.get("state") or {}
        response = JSONResponse(
            status_code=401,
            content={
                "error": "unauthorized",
                "reason": reason,
                "request_id": state.get("request_id"),
            },
        )
        await response(scope, receive, send)
