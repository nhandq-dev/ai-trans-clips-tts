from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings

# Fields attached via `extra=` on a log record that should be surfaced in the JSON output.
_REQUEST_FIELDS = (
    "request_id",
    "method",
    "path",
    "status_code",
    "duration_ms",
    "engine",
    "language",
    "text_length",
)


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON for stdout aggregation."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in _REQUEST_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(settings: Settings) -> None:
    """Install the JSON handler on the root logger and route uvicorn logs through it."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Let uvicorn loggers propagate to the root JSON handler instead of printing plain text.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    # Access logging is handled by RequestContextMiddleware.
    logging.getLogger("uvicorn.access").disabled = True
