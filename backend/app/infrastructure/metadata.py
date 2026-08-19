"""Service / build metadata (Task 8.10).

Centralises the application's operational metadata (service name,
environment, version, and optional build information) into a single
secrets-safe object. The version is read from ``pyproject.toml`` — the
single source of truth — via ``Settings._load_project_version()`` and
may be overridden at build time by the ``APP_VERSION`` environment
variable.

All fields are safe for logs, traces, and health endpoints (no
credentials, no secrets).
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.infrastructure.config import Settings


@dataclass(frozen=True)
class ServiceMetadata:
    """Secrets-safe operational metadata for the service.

    All fields are plain strings, safe for logs, traces, and health
    responses.  Build fields default to ``""`` when absent — callers
    check ``bool(value)`` to decide whether to include them.
    """

    service_name: str
    environment: str
    version: str
    build_commit: str = ""
    build_timestamp: str = ""

    def to_dict(self) -> dict[str, str]:
        """Flat dict representation (safe for JSON serialisation)."""
        return {
            "service_name": self.service_name,
            "environment": self.environment,
            "version": self.version,
            "build_commit": self.build_commit,
            "build_timestamp": self.build_timestamp,
        }


def load_service_metadata(settings: Settings) -> ServiceMetadata:
    """Build a ``ServiceMetadata`` from the application settings.

    Build info is present only when the corresponding environment
    variables (``BUILD_COMMIT``, ``BUILD_TIMESTAMP``) were set at
    deployment time.
    """
    return ServiceMetadata(
        service_name=settings.app_name,
        environment=settings.app_env,
        version=settings.app_version,
        build_commit=settings.build_commit or "",
        build_timestamp=settings.build_timestamp or "",
    )


__all__ = [
    "ServiceMetadata",
    "load_service_metadata",
]
