"""Unit tests for health check models and checks."""

from __future__ import annotations

import asyncio

import pytest

from backend.app.infrastructure.health.checks import run_check
from backend.app.infrastructure.health.models import (
    DependencyResult,
    DependencyStatus,
    HealthResponse,
    OverallStatus,
    ReadinessResponse,
)


class TestDependencyStatus:
    """Tests for the DependencyStatus enum."""

    def test_enum_values(self) -> None:
        assert DependencyStatus.OK.value == "ok"
        assert DependencyStatus.FAILED.value == "failed"
        assert DependencyStatus.TIMEOUT.value == "timeout"


class TestHealthResponse:
    """Tests for the HealthResponse model."""

    def test_default_status(self) -> None:
        response = HealthResponse(service="test", version="1.0")
        assert response.status == "ok"
        assert response.service == "test"
        assert response.version == "1.0"


class TestReadinessResponse:
    """Tests for the ReadinessResponse model."""

    def test_ready_response(self) -> None:
        deps = {
            "postgres": DependencyResult(status=DependencyStatus.OK),
            "redis": DependencyResult(status=DependencyStatus.OK),
        }
        response = ReadinessResponse(status=OverallStatus.READY, dependencies=deps)
        assert response.status == OverallStatus.READY
        assert response.dependencies["postgres"].status == DependencyStatus.OK

    def test_not_ready_response(self) -> None:
        deps = {
            "postgres": DependencyResult(status=DependencyStatus.OK),
            "redis": DependencyResult(status=DependencyStatus.FAILED),
        }
        response = ReadinessResponse(status=OverallStatus.NOT_READY, dependencies=deps)
        assert response.status == OverallStatus.NOT_READY
        assert response.dependencies["redis"].status == DependencyStatus.FAILED


class TestRunCheck:
    """Tests for the run_check function."""

    @pytest.mark.asyncio
    async def test_ok_status(self) -> None:
        async def _healthy() -> bool:
            return True

        result = await run_check("test_check", _healthy)
        assert result == DependencyStatus.OK

    @pytest.mark.asyncio
    async def test_failed_status(self) -> None:
        async def _unhealthy() -> bool:
            return False

        result = await run_check("test_check", _unhealthy)
        assert result == DependencyStatus.FAILED

    @pytest.mark.asyncio
    async def test_timeout_status(self) -> None:
        async def _slow() -> bool:
            await asyncio.sleep(10)
            return True

        result = await run_check("test_check", _slow, timeout=0.1)
        assert result == DependencyStatus.TIMEOUT

    @pytest.mark.asyncio
    async def test_exception_returns_failed(self) -> None:
        async def _broken() -> bool:
            msg = "test error"
            raise RuntimeError(msg)

        result = await run_check("test_check", _broken)
        assert result == DependencyStatus.FAILED

    @pytest.mark.asyncio
    async def test_not_ready_aggregation(self) -> None:
        """When one dependency fails, overall status is NOT_READY."""

        async def _healthy() -> bool:
            return True

        async def _unhealthy() -> bool:
            return False

        results = {
            "a": await run_check("a", _healthy),
            "b": await run_check("b", _unhealthy),
        }
        all_ok = all(r == DependencyStatus.OK for r in results.values())
        assert not all_ok

    @pytest.mark.asyncio
    async def test_all_ok_aggregation(self) -> None:
        """When all dependencies pass, overall status is READY."""

        async def _healthy() -> bool:
            return True

        results = {
            "a": await run_check("a", _healthy),
            "b": await run_check("b", _healthy),
        }
        all_ok = all(r == DependencyStatus.OK for r in results.values())
        assert all_ok
