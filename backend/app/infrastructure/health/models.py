"""Typed models for health and readiness responses."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

# Re-export builtin TimeoutError for use by checks.py
TimeoutError = TimeoutError


class DependencyStatus(StrEnum):
    """Status of an individual dependency check."""

    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"


class OverallStatus(StrEnum):
    """Overall readiness status."""

    READY = "ready"
    NOT_READY = "not_ready"


class DependencyResult(BaseModel):
    """Result of a single dependency health check."""

    status: DependencyStatus = Field(description="Status of the dependency check")


class ReadinessResponse(BaseModel):
    """Response model for /ready endpoint."""

    status: OverallStatus = Field(description="Overall readiness of the application")
    dependencies: dict[str, DependencyResult] = Field(
        description="Per-dependency health check results",
    )


class HealthResponse(BaseModel):
    """Response model for /health endpoint.

    /health only indicates the application process is alive.
    It does NOT check external dependencies.
    """

    status: str = Field(default="ok", description="Application liveness status")
    service: str = Field(description="Service name")
    version: str = Field(description="Application version")
    environment: str | None = Field(default=None, description="Deployment environment")
    build_commit: str | None = Field(default=None, description="Git commit hash")
    build_timestamp: str | None = Field(default=None, description="Build timestamp")
