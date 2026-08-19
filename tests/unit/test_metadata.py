"""Task 8.10 — Service / build metadata tests.

Covers the single version source from pyproject.toml, the ServiceMetadata
dataclass, build info propagation into logs, traces, and health responses.
"""

from __future__ import annotations

import json
import logging

import pytest

from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.metadata import ServiceMetadata, load_service_metadata


class TestProjectVersionSource:
    """Task 8.10 req 1-2: single version source from pyproject.toml."""

    def test_version_reads_from_pyproject(self) -> None:
        """Settings.app_version defaults to pyproject.toml version."""
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.app_version == "0.1.0", "pyproject.toml version is 0.1.0"

    def test_version_overridable_by_env(self) -> None:
        """APP_VERSION env var overrides the pyproject default."""
        settings = Settings(_env_file=None, APP_VERSION="2.0.0")  # type: ignore[call-arg]
        assert settings.app_version == "2.0.0"

    def test_no_duplicate_version_source(self) -> None:
        """The default value is NOT hardcoded in Settings (uses default_factory)."""
        # If the default were hardcoded, changing the default factory would
        # not affect the field.  This test verifies the factory is wired.
        assert Settings.model_fields["app_version"].default_factory is not None


class TestServiceMetadata:
    """Task 8.10: ServiceMetadata dataclass centralises metadata."""

    def test_from_settings(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        meta = load_service_metadata(settings)
        assert isinstance(meta, ServiceMetadata)
        assert meta.service_name == "HotelOps AI"
        assert meta.environment == "development"
        assert meta.version == "0.1.0"
        assert meta.build_commit == ""  # absent by default
        assert meta.build_timestamp == ""

    def test_build_info_present(self) -> None:
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None, BUILD_COMMIT="abc123", BUILD_TIMESTAMP="2026-08-10T12:00:00Z"
        )
        meta = load_service_metadata(settings)
        assert meta.build_commit == "abc123"
        assert meta.build_timestamp == "2026-08-10T12:00:00Z"

    def test_to_dict(self) -> None:
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None, BUILD_COMMIT="abc123", BUILD_TIMESTAMP="ts"
        )
        d = load_service_metadata(settings).to_dict()
        assert d["service_name"] == "HotelOps AI"
        assert d["environment"] == "development"
        assert d["version"] == "0.1.0"
        assert d["build_commit"] == "abc123"
        assert d["build_timestamp"] == "ts"
        # No secrets — all string fields
        assert all(isinstance(v, str) for v in d.values())

    def test_frozen(self) -> None:
        meta = ServiceMetadata(service_name="s", environment="e", version="v")
        with pytest.raises(AttributeError):
            meta.service_name = "other"  # type: ignore[misc]


class TestBuildInfoInLogs:
    """Task 8.10 req 3: build metadata available in structured logs."""

    def _format(self, **settings_overrides: str) -> dict:
        from backend.app.infrastructure.logging import JsonFormatter

        settings = Settings(_env_file=None, **settings_overrides)  # type: ignore[call-arg]
        formatter = JsonFormatter(
            service=settings.app_name,
            environment=settings.app_env,
            version=settings.app_version,
            build_commit=settings.build_commit or "",
            build_timestamp=settings.build_timestamp or "",
        )
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello",
            args=(),
            exc_info=None,
        )
        return json.loads(formatter.format(record))

    def test_build_commit_in_json_log(self) -> None:
        payload = self._format(BUILD_COMMIT="abc123")
        assert payload["build_commit"] == "abc123"

    def test_build_timestamp_in_json_log(self) -> None:
        payload = self._format(BUILD_TIMESTAMP="2026-08-10T12:00:00Z")
        assert payload["build_timestamp"] == "2026-08-10T12:00:00Z"

    def test_build_absent_when_not_set(self) -> None:
        payload = self._format()
        assert "build_commit" not in payload
        assert "build_timestamp" not in payload

    def test_build_info_in_text_log(self) -> None:
        from backend.app.infrastructure.logging import TextFormatter

        settings = Settings(  # type: ignore[call-arg]
            _env_file=None, BUILD_COMMIT="def456", BUILD_TIMESTAMP="ts"
        )
        formatter = TextFormatter(
            service=settings.app_name,
            environment=settings.app_env,
            version=settings.app_version,
            build_commit=settings.build_commit or "",
            build_timestamp=settings.build_timestamp or "",
        )
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello",
            args=(),
            exc_info=None,
        )
        line = formatter.format(record)
        assert "build_commit='def456'" in line
        assert "build_timestamp='ts'" in line


class TestBuildInfoInTraces:
    """Task 8.10 req 4: build metadata available in tracing resources."""

    def test_build_commit_on_trace_resource(self) -> None:
        """Build info appears on span resources when configured.

        Exercises the PRODUCTION resource-attribute construction
        (``tracing.resource_attributes`` — the same function
        ``configure_tracing`` uses) with a LOCAL TracerProvider so the
        process-global provider is never touched (the OTel global
        provider is installed once per process, so this cannot
        interfere with the module-scoped fixture in test_tracing.py).
        """
        from opentelemetry.sdk.resources import (
            DEPLOYMENT_ENVIRONMENT,
            SERVICE_NAME,
            SERVICE_VERSION,
            Resource,
        )
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        from backend.app.infrastructure.observability import tracing

        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            OBSERVABILITY_TRACING_ENABLED=True,
            OTEL_SAMPLE_RATIO=1.0,
            BUILD_COMMIT="abc123",
            BUILD_TIMESTAMP="2026-08-10T12:00:00Z",
        )
        # Use the same construction configure_tracing() uses.
        resource = Resource.create(tracing.resource_attributes(settings))
        exporter = InMemorySpanExporter()
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        with provider.get_tracer("meta-test").start_as_current_span("meta-test"):
            pass
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        resource_attrs = spans[0].resource.attributes
        assert resource_attrs["build.commit"] == "abc123"
        assert resource_attrs["build.timestamp"] == "2026-08-10T12:00:00Z"
        assert resource_attrs[SERVICE_NAME] == "hotelops-ai"
        assert resource_attrs[SERVICE_VERSION] == "0.1.0"
        assert resource_attrs[DEPLOYMENT_ENVIRONMENT] == "development"


class TestBuildInfoInHealthEndpoint:
    """Task 8.10 req 5: build metadata available in health responses."""

    def test_health_response_includes_build(self) -> None:
        from backend.app.infrastructure.health.models import HealthResponse

        response = HealthResponse(
            status="ok",
            service="HotelOps AI",
            version="0.1.0",
            environment="production",
            build_commit="abc123",
            build_timestamp="2026-08-10T12:00:00Z",
        )
        data = response.model_dump()
        assert data["build_commit"] == "abc123"
        assert data["build_timestamp"] == "2026-08-10T12:00:00Z"
        assert data["environment"] == "production"

    def test_health_response_build_optional(self) -> None:
        from backend.app.infrastructure.health.models import HealthResponse

        response = HealthResponse(
            status="ok",
            service="HotelOps AI",
            version="0.1.0",
        )
        data = response.model_dump()
        assert data["build_commit"] is None
        assert data["build_timestamp"] is None
        assert data["environment"] is None


class TestSecretsSafe:
    """Task 8.10 req 6: no secrets in metadata."""

    def test_metadata_to_dict_no_secrets(self) -> None:
        meta = ServiceMetadata(service_name="test", environment="test", version="1.0")
        d = meta.to_dict()
        assert "password" not in str(d)
        assert "token" not in str(d).lower()
        assert "secret" not in str(d).lower()
        assert "credential" not in str(d).lower()
