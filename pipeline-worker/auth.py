"""HMAC auth middleware for the pipeline worker.

Pure ASGI (not BaseHTTPMiddleware) because the body must be buffered once for
signature verification and then replayed to the downstream app — reading it in a
BaseHTTPMiddleware would consume the stream the routes need.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable

from security import SignatureError, SignatureVerifier, parse_keys
from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("pipeline-worker.access")

# Routes that must answer without a signature: container/orchestrator probes.
PUBLIC_PATHS = ("/health",)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign/echo a request id and emit one access log line per request."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._header = os.getenv("REQUEST_ID_HEADER", "X-Request-Id")
        self._log_enabled = os.getenv("ACCESS_LOG_ENABLED", "true").lower() == "true"

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get(self._header) or uuid.uuid4().hex
        request.state.request_id = request_id

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            self._log(request, None, start)
            raise
        response.headers[self._header] = request_id
        self._log(request, response.status_code, start)
        return response

    def _log(self, request: Request, status_code: int | None, start: float) -> None:
        if not self._log_enabled:
            return
        logger.info(
            "request",
            extra={
                "request_id": getattr(request.state, "request_id", None),
                "method": request.method,
                "path": request.url.path,
                "status_code": status_code,
                "duration_ms": round((time.perf_counter() - start) * 1000, 2),
            },
        )


class HMACAuthMiddleware:
    """Verify ``X-Pipeline-*`` HMAC headers before the request reaches the routes."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app
        self._verifier = SignatureVerifier(
            keys=parse_keys(os.getenv("HMAC_KEYS_JSON", "")),
            max_skew_seconds=int(os.getenv("HMAC_MAX_SKEW_SECONDS", "60")),
            nonce_ttl_seconds=int(os.getenv("HMAC_NONCE_TTL_SECONDS", "300")),
        )
        self._max_body_bytes = int(os.getenv("REQUEST_MAX_BODY_BYTES", str(2 * 1024 * 1024)))
        if not self._verifier.configured:
            logger.error("HMAC_KEYS_JSON is not configured — every signed route will reject")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path", "").startswith(PUBLIC_PATHS):
            await self._app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        declared = headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self._max_body_bytes:
            await self._error(
                scope, receive, send, 413, "payload_too_large", "request body too large"
            )
            return

        body = await self._read_body(receive)
        if body is None:
            await self._error(
                scope, receive, send, 413, "payload_too_large", "request body too large"
            )
            return

        try:
            self._verifier.verify(
                method=scope.get("method", ""),
                path_and_query=self._path_and_query(scope),
                headers=headers,
                body=body,
            )
        except SignatureError as exc:
            await self._error(scope, receive, send, 401, "unauthorized", str(exc))
            return

        await self._app(scope, self._replay(body, receive), send)

    async def _read_body(self, receive: Receive) -> bytes | None:
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.request":
                body.extend(message.get("body", b""))
                if len(body) > self._max_body_bytes:
                    return None
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break
        return bytes(body)

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
    async def _error(
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        error: str,
        reason: str,
    ) -> None:
        state = scope.get("state") or {}
        response = JSONResponse(
            status_code=status_code,
            content={"error": error, "reason": reason, "request_id": state.get("request_id")},
        )
        await response(scope, receive, send)
