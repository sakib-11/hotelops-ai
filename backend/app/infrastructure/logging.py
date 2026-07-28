"""Structured logging baseline for HotelOps AI.

Provides a configured logger with structured fields suitable
for JSON-serializable log output in production.
"""

from __future__ import annotations

import logging
import sys
from typing import Any


class StructFormatter(logging.Formatter):
    """Simple structured formatter with consistent field ordering."""

    def format(self, record: logging.LogRecord) -> str:
        fields: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        exc_info = record.exc_info
        if exc_info and exc_info[0]:
            fields["exception"] = self.formatException(exc_info)

        # Include extra context fields if present
        for key in (
            "request_id",
            "correlation_id",
            "trace_id",
            "tenant_id",
            "venue_id",
            "camera_id",
            "job_id",
            "event_id",
        ):
            value = getattr(record, key, None)
            if value is not None:
                fields[key] = value

        return " | ".join(f"{k}={v!r}" for k, v in fields.items())


def configure_logging(level: str = "INFO") -> None:
    """Configure root logger with structured formatting.

    Args:
        level: Log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructFormatter())

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Remove default handlers and add our structured handler
    root.handlers.clear()
    root.addHandler(handler)

    # Set third-party loggers to a quieter level
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
