from __future__ import annotations

import base64
import hmac
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

_HEALTH_PREFIX = "/health"
_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


def _is_docs_path(path: str) -> bool:
    """Return True for the interactive docs and the OpenAPI schema."""
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in _DOCS_PATHS)


def _is_hmac_exempt(path: str) -> bool:
    """Paths that skip HMAC: health probes and the password-gated docs."""
    return path.startswith(_HEALTH_PREFIX) or _is_docs_path(path)


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


class DocsAuthMiddleware:
    """Password-protect the interactive API docs.

    `/docs`, `/redoc`, and `/openapi.json` are exempt from HMAC so a browser can load them, and
    are instead gated by HTTP Basic auth whenever `DOCS_PASSWORD` is set. Production requires a
    password (enforced in `Settings`), so the docs stay reachable without being public. When no
    password is configured the docs are left open (development convenience).
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self._app = app
        self._password = settings.docs_password

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or not self._password
            or not _is_docs_path(scope.get("path", ""))
        ):
            await self._app(scope, receive, send)
            return

        if self._authorized(Headers(scope=scope)):
            await self._app(scope, receive, send)
            return

        response = Response(
            content="Unauthorized",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="docs"'},
        )
        await response(scope, receive, send)

    def _authorized(self, headers: Headers) -> bool:
        scheme, _, credentials = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "basic" or not credentials:
            return False
        try:
            decoded = base64.b64decode(credentials, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        _, separator, password = decoded.partition(":")
        if not separator:
            return False
        return hmac.compare_digest(password, self._password)


class HMACAuthMiddleware:
    """Verify HMAC-signed requests before they reach the routes.

    `/health/*` stays unauthenticated so liveness/readiness probes need no signature. The request
    body is buffered once, capped at `REQUEST_MAX_BODY_BYTES`, verified, and then replayed to the
    downstream application.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self._app = app
        self._verifier = SignatureVerifier(settings)
        self._max_body_bytes = settings.request_max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _is_hmac_exempt(scope.get("path", "")):
            await self._app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        declared_length = headers.get("content-length")
        if (
            declared_length
            and declared_length.isdigit()
            and int(declared_length) > self._max_body_bytes
        ):
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
        """Buffer the request body, returning None as soon as it exceeds the configured cap.

        The cap is enforced while reading (not after) so a chunked request without a
        `Content-Length` header cannot exhaust memory.
        """
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
            content={
                "error": error,
                "reason": reason,
                "request_id": state.get("request_id"),
            },
        )
        await response(scope, receive, send)
