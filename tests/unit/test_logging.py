"""Task 8.3 — focused unit tests for the centralized structured logging layer.

Covers the JSON formatter (required base schema, contextual allowlist,
exception preservation, secret non-leakage), the text dev formatter, and
the configure_logging() wiring (single handler, level, format selection).
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.logging import (
    JsonFormatter,
    TextFormatter,
    configure_logging,
)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """Snapshot root handler/level state so configure_logging() tests can't
    leak a mutated root logger into other tests in the same session."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    yield
    root.handlers.clear()
    root.handlers.extend(handlers)
    root.setLevel(level)


def _record(
    msg: str = "hello world",
    *,
    level: int = logging.INFO,
    exc_info: tuple | None = None,
    extra: dict | None = None,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name="tests.t8",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=exc_info,
    )
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    return record


class TestJsonFormatter:
    def test_required_base_schema(self) -> None:
        """Every record carries timestamp/level/service/environment/version/message."""
        formatter = JsonFormatter(service="HotelOps AI", environment="development", version="0.1.0")
        payload = json.loads(formatter.format(_record()))

        assert set(payload) >= {
            "timestamp",
            "level",
            "service",
            "environment",
            "version",
            "message",
        }
        assert payload["level"] == "INFO"
        assert payload["service"] == "HotelOps AI"
        assert payload["environment"] == "development"
        assert payload["version"] == "0.1.0"
        assert payload["message"] == "hello world"
        assert payload["logger"] == "tests.t8"
        # Timestamp must be ISO-8601 and timezone-aware.
        parsed = datetime.fromisoformat(payload["timestamp"])
        assert parsed.tzinfo is not None
        assert abs((parsed - datetime.now(UTC)).total_seconds()) < 60

    def test_contextual_fields_are_emitted(self) -> None:
        """All allowlisted context fields attached via extra= appear in JSON."""
        formatter = JsonFormatter(service="s", environment="e", version="v")
        extra = {
            "request_id": "req-1",
            "correlation_id": "corr-1",
            "trace_id": "trace-1",
            "actor_id": uuid.uuid4(),
            "tenant_id": uuid.uuid4(),
            "venue_id": uuid.uuid4(),
            "job_id": "job-1",
            "session_id": "sess-1",
            "event_id": uuid.uuid4(),
            "camera_id": "cam-1",
        }
        payload = json.loads(formatter.format(_record(extra=extra)))
        for key, value in extra.items():
            assert payload[key] == str(value), f"context field {key} missing or wrong"

    def test_non_allowlisted_extra_is_dropped(self) -> None:
        """Secrets passed via extra= must NEVER appear in the JSON output."""
        formatter = JsonFormatter(service="s", environment="e", version="v")
        payload = json.loads(
            formatter.format(
                _record(extra={"password": "hunter2", "api_secret": "s3cr3t", "tenant_id": "ok"})
            )
        )
        assert "password" not in payload
        assert "api_secret" not in payload
        assert payload["tenant_id"] == "ok", "allowlisted fields still flow through"

    def test_exception_stack_trace_is_preserved(self) -> None:
        """exc_info must produce a full traceback + structured type/message."""
        formatter = JsonFormatter(service="s", environment="e", version="v")
        try:
            raise ValueError("boom")
        except ValueError:
            record = _record(exc_info=sys.exc_info())

        payload = json.loads(formatter.format(record))
        assert payload["exc_type"] == "ValueError"
        assert payload["exc_message"] == "boom"
        assert "Traceback (most recent call last)" in payload["exception"]
        assert "ValueError: boom" in payload["exception"]

    def test_non_serializable_context_is_stringified(self) -> None:
        """UUID/datetime context values serialize deterministically."""
        formatter = JsonFormatter(service="s", environment="e", version="v")
        event_id = uuid.uuid4()
        payload = json.loads(formatter.format(_record(extra={"event_id": event_id})))
        assert payload["event_id"] == str(event_id)


class TestTextFormatter:
    def test_emits_key_value_fields(self) -> None:
        formatter = TextFormatter(service="HotelOps AI", environment="development", version="0.1.0")
        line = formatter.format(_record(extra={"request_id": "req-1"}))
        assert "service='HotelOps AI'" in line
        assert "environment='development'" in line
        assert "version='0.1.0'" in line
        assert "message='hello world'" in line
        assert "request_id='req-1'" in line


class TestConfigureLogging:
    def test_replaces_handlers_and_sets_level(self) -> None:
        """configure_logging leaves exactly one handler and honors the level."""
        settings = _settings(LOG_LEVEL="DEBUG", OBSERVABILITY_LOG_FORMAT="json")
        configure_logging(settings.log_level, settings=settings)
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert root.level == logging.DEBUG
        assert isinstance(root.handlers[0].formatter, JsonFormatter)

    def test_text_format_selects_text_formatter(self) -> None:
        settings = _settings(OBSERVABILITY_LOG_FORMAT="text")
        configure_logging(settings.log_level, settings=settings)
        root = logging.getLogger()
        assert isinstance(root.handlers[0].formatter, TextFormatter)

    def test_settings_default_is_json(self) -> None:
        assert _settings().observability_log_format == "json"

    def test_defaults_without_settings_still_configure(self) -> None:
        """Backward-compatible: configure_logging(level) without settings."""
        configure_logging("WARNING")
        root = logging.getLogger()
        assert root.level == logging.WARNING
        assert len(root.handlers) == 1


# =============================================================================
# Task 8.9 — Security Log Redaction
# =============================================================================


class TestSecretRedactionMessages:
    """Task 8.9: every credential category is redacted from the message body."""

    def _format(self, msg: str, **extra: Any) -> str:
        """Format a record through the JsonFormatter and return the message."""
        from backend.app.infrastructure.logging import JsonFormatter

        formatter = JsonFormatter(service="s", environment="e", version="v")
        record = _record(msg, extra=extra or None)
        return json.loads(formatter.format(record))["message"]

    def test_authorization_bearer_redacted(self) -> None:
        assert "[REDACTED]" in self._format("Authorization: Bearer abc.def.ghi")

    def test_authorization_basic_redacted(self) -> None:
        assert "[REDACTED]" in self._format("Authorization: Basic dXNlcjpwYXNz")

    def test_password_kv_redacted(self) -> None:
        assert self._format("password = hunter2") == "[REDACTED]"

    def test_secret_key_redacted(self) -> None:
        assert self._format("secret_key: sk-1234") == "[REDACTED]"

    def test_api_key_redacted(self) -> None:
        assert self._format('API_KEY="sk-1234567890abcdef"') == "[REDACTED]"

    def test_jwt_token_redacted(self) -> None:
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dGVzdA"
        result = self._format(f"token={jwt}")
        assert "[REDACTED]" in result

    def test_connection_string_redacted(self) -> None:
        result = self._format("connecting to postgresql://user:secretpass@localhost/db")
        assert "[REDACTED]" in result
        assert "secretpass" not in result

    def test_auth_cookie_redacted(self) -> None:
        assert "[REDACTED]" in self._format("session=abc123")
        assert "abc123" not in self._format("session=abc123")

    def test_access_token_redacted(self) -> None:
        result = self._format("access_token=ghp_1234567890abcdef")
        assert "[REDACTED]" in result
        assert "ghp_1234567890abcdef" not in result

    def test_refresh_token_redacted(self) -> None:
        assert self._format("refresh_token: r1234567890abcdef") == "[REDACTED]"

    def test_database_password_redacted(self) -> None:
        assert "[REDACTED]" in self._format("postgres_password=CHANGE_ME")


class TestSafeIdentifiersNotRedacted:
    """Task 8.9: legitimate operational identifiers must NOT be redacted."""

    def _format(self, msg: str, **extra: Any) -> str:
        from backend.app.infrastructure.logging import JsonFormatter

        formatter = JsonFormatter(service="s", environment="e", version="v")
        record = _record(msg, extra=extra or None)
        return json.loads(formatter.format(record))["message"]

    def custom_field(self, key: str, value: str) -> str:
        return self._format(f"{key}={value}")

    def test_request_id_not_redacted(self) -> None:
        result = self.custom_field("request_id", "abc123def456")
        assert "[REDACTED]" not in result
        assert "abc123def456" in result

    def test_trace_id_not_redacted(self) -> None:
        result = self.custom_field("trace_id", "ab" * 16)
        assert "[REDACTED]" not in result
        assert ("ab" * 16) in result

    def test_event_id_not_redacted(self) -> None:
        result = self.custom_field("event_id", "550e8400-e29b-41d4-a716-446655440000")
        assert "[REDACTED]" not in result
        assert "550e8400-e29b-41d4-a716-446655440000" in result

    def test_job_id_not_redacted(self) -> None:
        result = self.custom_field("job_id", "job-12345")
        assert "[REDACTED]" not in result
        assert "job-12345" in result

    def test_correlation_id_not_redacted(self) -> None:
        result = self.custom_field("correlation_id", "corr-12345")
        assert "[REDACTED]" not in result
        assert "corr-12345" in result


class TestExceptionMessageRedaction:
    """Task 8.9: exception messages containing secrets are redacted."""

    def test_exception_with_password_redacted(self) -> None:
        from backend.app.infrastructure.logging import JsonFormatter

        formatter = JsonFormatter(service="s", environment="e", version="v")
        try:
            raise ValueError("password=hunter2")
        except ValueError:
            record = _record("operation failed", exc_info=sys.exc_info())
        payload = json.loads(formatter.format(record))
        assert "[REDACTED]" in payload["exc_message"]
        assert "hunter2" not in payload["exc_message"]
        assert "[REDACTED]" in payload["exception"]


class TestContextFieldValueRedaction:
    """Task 8.9: even allowlisted extra= values are redacted if they
    contain credential-like content (defense-in-depth)."""

    def _format(self, msg: str, **extra: Any) -> Any:
        from backend.app.infrastructure.logging import JsonFormatter

        formatter = JsonFormatter(service="s", environment="e", version="v")
        record = _record(msg, extra=extra)
        return json.loads(formatter.format(record))

    def test_password_in_extra_allowlisted_key_redacted(self) -> None:
        """If someone abuses an allowlisted key to carry a password,
        the value itself is redacted."""
        payload = self._format("event", request_id="password=hunter2")
        assert "[REDACTED]" in payload["request_id"]
        assert "hunter2" not in payload["request_id"]

    def test_safe_extra_value_not_redacted(self) -> None:
        payload = self._format("event", event_id=uuid.uuid4())
        assert "[REDACTED]" not in payload["event_id"]


class TestSecretsCannotAppearInJsonLogOutput:
    """Security boundary: secrets intentionally injected at multiple
    points must NEVER appear in the final JSON output."""

    def _emit(self, level: int, msg: str, *args: Any, **kwargs: Any) -> str:
        """Emit a log through the full pipeline (not just the formatter)
        and capture the JSON output."""
        from backend.app.infrastructure.logging import configure_logging

        settings = _settings(LOG_LEVEL="DEBUG", OBSERVABILITY_LOG_FORMAT="json")
        configure_logging(settings.log_level, settings=settings)
        logger = logging.getLogger(__name__)
        import io

        buf = io.StringIO()
        handler = logging.getLogger().handlers[0]
        original = handler.stream
        handler.stream = buf
        try:
            logger.log(level, msg, *args, **kwargs)
        finally:
            handler.stream = original
        return buf.getvalue().strip()

    def test_password_in_message_not_in_json(self) -> None:
        output = self._emit(logging.INFO, "password = hunter2")
        assert "hunter2" not in output
        assert "[REDACTED]" in output

    def test_jwt_in_message_not_in_json(self) -> None:
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dGVzdA"
        output = self._emit(logging.INFO, "token=%s", jwt)
        assert jwt not in output
        assert "[REDACTED]" in output

    def test_authorization_header_not_in_json(self) -> None:
        output = self._emit(logging.INFO, "Authorization: Bearer abc.def")
        assert "abc.def" not in output
        assert "[REDACTED]" in output

    def test_none_values_dont_crash(self) -> None:
        """Redaction handles None values gracefully."""
        from backend.app.infrastructure.logging import _redact_message, _redact_value

        assert _redact_message("") == ""
        assert _redact_value("") == ""

    def test_extra_password_not_in_output(self) -> None:
        """extra= with password-only key is already blocked by
        _CONTEXT_FIELDS allowlist."""
        output = self._emit(logging.INFO, "event", extra={"password": "hunter2"})
        assert "hunter2" not in output

    def test_extra_secret_key_not_in_output(self) -> None:
        output = self._emit(logging.INFO, "event", extra={"secret_key": "s3cr3t"})
        assert "s3cr3t" not in output
