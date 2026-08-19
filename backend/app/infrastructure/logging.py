"""Centralized structured logging for HotelOps AI (Task 8.3).

One configuration module owns every logger in the process. The default
output is one JSON object per line (``OBSERVABILITY_LOG_FORMAT=json``)
so logs are directly consumable by log shippers/Grafana; ``text`` keeps
the human-readable key=value format for local development.

Every record carries a fixed base schema:

    timestamp, level, service, environment, version, logger, message

plus any of the contextual fields the caller attaches via ``extra=``
(request_id, correlation_id, trace_id, actor_id, tenant_id, venue_id,
job_id, session_id, event_id, camera_id). Only these allowlisted keys
are ever emitted — application code never constructs JSON manually, and
an arbitrary ``extra=`` key (e.g. ``password``) is silently dropped, so
secrets cannot leak through the contextual-field path. Exception stack
traces are preserved under ``exception`` (plus ``exc_type`` and
``exc_message``) with all secret-like content redacted.

Task 8.9 — Security Log Redaction: all log messages, exception fields,
and context-field values pass through a single ``_redact_message()``
function before leaving the process. This is the one controlled
redaction mechanism (not scattered through the application).

Callers keep using the stdlib logging API::

    logger.info("event enqueued", extra={"event_id": str(event_id), "tenant_id": str(tid)})

No print statements, no manual json.dumps in application code.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import traceback
from datetime import UTC, datetime
from typing import Any

from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.observability.context import get_request_context

# Contextual fields allowed to flow into log records (Task 8.3 §7).
# Anything else passed via ``extra=`` is NOT logged — this allowlist is
# the first line of defense against secret leakage (requirement 10).
_CONTEXT_FIELDS = (
    "request_id",
    "correlation_id",
    "trace_id",
    "span_id",
    "actor_id",
    "tenant_id",
    "venue_id",
    "job_id",
    "session_id",
    "event_id",
    "evidence_id",
    "camera_id",
)

# =============================================================================
# Task 8.9 — Security Log Redaction (one controlled mechanism)
# =============================================================================
#
# Compiled regex patterns covering every credential category. Applied to
# the message body, exception fields, and context-field values before
# they leave the process. These patterns are designed NOT to match
# legitimate operational identifiers (request_id, trace_id, event_id,
# job_id, etc.) which are UUIDs or short hex strings — they do not match
# the credential-key patterns below.

_REDACTED = "[REDACTED]"

# Authorization header values (Bearer <token>, Basic <base64>).
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization:\s*(?:bearer\s+|basic\s+)?)\S+")

# JWT tokens (three base64url segments, first segment starts with eyJ).
_JWT_RE = re.compile(r"eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]+")

# Credential key=value / key:value / "key": "value" patterns.
# Matches the key name and replaces the value portion after the separator.
_CREDENTIAL_VALUE_RE = re.compile(
    r"(?i)"
    r"(?:"
    r"(?:password|passwd|secret|api[_-]?key|apikey)"
    r"|(?:access[_-]?key|secret[_-]?key|private[_-]?key)"
    r"|(?:auth[_-]?token|refresh[_-]?token|id[_-]?token)"
    r"|(?:token|credential|postgres[_-]?password|redis[_-]?password)"
    r")"
    r"\s*[=:}]\s*"
    r"'?(?![\[\]{}])\S{4,}'?"
)

# Connection strings with embedded credentials: proto://user:pass@host.
_CONNECTION_STRING_RE = re.compile(r"(\w+://)\S+:\S+@")

# Cookie values containing session/auth identifiers.
# Only matches cookies whose key suggests authentication (not generic cookies).
_AUTH_COOKIE_RE = re.compile(r"(?i)((?:session|auth|token|sid|connect\.sid)[=:])\s*\S+")


def _redact_message(message: str) -> str:
    """Redact secrets from a log message (Task 8.9).

    Applies all credential patterns to the input string and returns the
    safe version. This is the single redaction function — application
    code never calls it directly (it is applied centrally by the
    formatters at the output boundary).

    Non-matching text (including legitimate operational identifiers:
    request_id, trace_id, event_id, job_id, etc.) passes through
    unchanged.
    """
    if not isinstance(message, str) or not message:
        return message
    message = _AUTH_HEADER_RE.sub(rf"\1{_REDACTED}", message)
    message = _JWT_RE.sub(_REDACTED, message)
    message = _CREDENTIAL_VALUE_RE.sub(_REDACTED, message)
    message = _CONNECTION_STRING_RE.sub(rf"\1{_REDACTED}:{_REDACTED}@", message)
    message = _AUTH_COOKIE_RE.sub(rf"\1{_REDACTED}", message)
    return message


def _redact_value(value: Any) -> str:
    """Redact a single value (used for context field values)."""
    if not isinstance(value, str):
        value = str(value)
    return _redact_message(value)


def _iso_timestamp(record: logging.LogRecord) -> str:
    """ISO-8601 UTC timestamp derived from the record's creation time."""
    return datetime.fromtimestamp(record.created, tz=UTC).isoformat()


def _base_entry(
    record: logging.LogRecord,
    *,
    service: str,
    environment: str,
    version: str,
    build_commit: str = "",
    build_timestamp: str = "",
) -> dict[str, Any]:
    """The shared record schema: base fields + allowlisted context fields.

    All text values (message and context fields) pass through
    ``_redact_message`` before leaving the process — this is the single
    controlled redaction mechanism (Task 8.9). Build metadata is
    included when available (empty string = absent).
    """
    entry: dict[str, Any] = {
        "timestamp": _iso_timestamp(record),
        "level": record.levelname,
        "service": service,
        "environment": environment,
        "version": version,
        "logger": record.name,
        "message": _redact_message(record.getMessage()),
    }
    if build_commit:
        entry["build_commit"] = build_commit
    if build_timestamp:
        entry["build_timestamp"] = build_timestamp
    for key in _CONTEXT_FIELDS:
        value = getattr(record, key, None)
        if value is not None:
            entry[key] = _redact_value(value)
    return entry


def _attach_exception(entry: dict[str, Any], record: logging.LogRecord) -> None:
    """Attach the formatted traceback + structured type/message (if any).

    Exception message and the full traceback are redacted through the
    same ``_redact_message`` function (Task 8.9) so that credentials
    within exception strings cannot leak into logs.
    """
    if record.exc_info is None or record.exc_info[0] is None:
        return
    exc_type, exc_value, exc_tb = record.exc_info
    entry["exc_type"] = exc_type.__name__
    if exc_value is not None:
        entry["exc_message"] = _redact_message(str(exc_value))
    entry["exception"] = _redact_message(
        "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    )


class ContextFilter(logging.Filter):
    """Injects the active request context into every log record.

    Reads the task-local request context (Task 8.4) and attaches its
    fields to the record, so application code never needs to pass
    request_id/correlation_id/trace_id via ``extra=`` — the centralized
    logger adds them automatically. Outside a request the context is
    empty and the filter is a no-op. Existing record attributes are
    never overwritten (explicit ``extra=`` wins).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in get_request_context().items():
            if value is not None and not hasattr(record, key):
                setattr(record, key, value)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per log line, with a stable field order."""

    def __init__(
        self,
        *,
        service: str,
        environment: str,
        version: str,
        build_commit: str = "",
        build_timestamp: str = "",
    ) -> None:
        super().__init__()
        self._service = service
        self._environment = environment
        self._version = version
        self._build_commit = build_commit
        self._build_timestamp = build_timestamp

    def format(self, record: logging.LogRecord) -> str:
        entry = _base_entry(
            record,
            service=self._service,
            environment=self._environment,
            version=self._version,
            build_commit=self._build_commit,
            build_timestamp=self._build_timestamp,
        )
        _attach_exception(entry, record)
        # default=str keeps records serializable even if a context value
        # is a UUID/datetime/enum; separators keep lines compact.
        return json.dumps(entry, ensure_ascii=False, separators=(",", ":"), default=str)


class TextFormatter(logging.Formatter):
    """Human-readable key=value output for local development."""

    def __init__(
        self,
        *,
        service: str,
        environment: str,
        version: str,
        build_commit: str = "",
        build_timestamp: str = "",
    ) -> None:
        super().__init__()
        self._service = service
        self._environment = environment
        self._version = version
        self._build_commit = build_commit
        self._build_timestamp = build_timestamp

    def format(self, record: logging.LogRecord) -> str:
        entry = _base_entry(
            record,
            service=self._service,
            environment=self._environment,
            version=self._version,
            build_commit=self._build_commit,
            build_timestamp=self._build_timestamp,
        )
        _attach_exception(entry, record)
        return " | ".join(f"{key}={value!r}" for key, value in entry.items())


def _formatter_for(log_format: str, settings: Settings) -> logging.Formatter:
    """Build the formatter selected by OBSERVABILITY_LOG_FORMAT."""
    kwargs: dict[str, Any] = {
        "service": settings.app_name,
        "environment": settings.app_env,
        "version": settings.app_version,
        "build_commit": settings.build_commit or "",
        "build_timestamp": settings.build_timestamp or "",
    }
    if log_format == "json":
        return JsonFormatter(**kwargs)
    return TextFormatter(**kwargs)


def configure_logging(level: str = "INFO", *, settings: Settings | None = None) -> None:
    """Configure the root logger with the centralized structured formatter.

    Replaces any existing handlers with a single stream handler, so
    there is exactly one logging configuration per process.

    Args:
        level: Log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        settings: Application settings (service/environment/version and
            the selected OBSERVABILITY_LOG_FORMAT). Falls back to the
            default Settings when not provided.
    """
    resolved = settings if settings is not None else Settings()  # type: ignore[call-arg]
    log_format = resolved.observability_log_format

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_formatter_for(log_format, resolved))
    # Auto-attach the active request context (request_id, correlation_id,
    # trace_id, ...) to every record (Task 8.4).
    handler.addFilter(ContextFilter())

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Remove default handlers and add our structured handler — one
    # centralized configuration, no duplicate/leftover handlers.
    root.handlers.clear()
    root.addHandler(handler)

    # Keep third-party loggers quiet by default (they are still JSON-
    # formatted when they do emit).
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)


__all__ = [
    "ContextFilter",
    "JsonFormatter",
    "TextFormatter",
    "configure_logging",
]
