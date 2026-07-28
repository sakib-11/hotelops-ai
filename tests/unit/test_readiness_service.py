"""Unit tests for ReadinessService."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from backend.app.infrastructure.database.client import DatabaseClient
from backend.app.infrastructure.health.models import DependencyStatus, OverallStatus
from backend.app.infrastructure.health.service import ReadinessService
from backend.app.infrastructure.redis.client import RedisClient
from backend.app.infrastructure.storage.client import StorageClient


@pytest.fixture
def mock_clients() -> tuple[DatabaseClient, RedisClient, StorageClient]:
    """Create mock infrastructure clients."""
    db = AsyncMock(spec=DatabaseClient)
    redis = AsyncMock(spec=RedisClient)
    storage = AsyncMock(spec=StorageClient)
    return db, redis, storage


class TestReadinessService:
    """Tests for ReadinessService orchestration."""

    @pytest.mark.asyncio
    async def test_all_healthy(self, mock_clients: tuple) -> None:
        db, redis, storage = mock_clients
        db.check_connectivity.return_value = True
        redis.check_connectivity.return_value = True
        storage.check_connectivity.return_value = True

        service = ReadinessService(db, redis, storage)
        result = await service.check_readiness()

        assert result.status == OverallStatus.READY
        assert result.dependencies["postgres"].status == DependencyStatus.OK
        assert result.dependencies["redis"].status == DependencyStatus.OK
        assert result.dependencies["object_storage"].status == DependencyStatus.OK

    @pytest.mark.asyncio
    async def test_database_fails(self, mock_clients: tuple) -> None:
        db, redis, storage = mock_clients
        db.check_connectivity.return_value = False
        redis.check_connectivity.return_value = True
        storage.check_connectivity.return_value = True

        service = ReadinessService(db, redis, storage)
        result = await service.check_readiness()

        assert result.status == OverallStatus.NOT_READY
        assert result.dependencies["postgres"].status == DependencyStatus.FAILED

    @pytest.mark.asyncio
    async def test_redis_fails(self, mock_clients: tuple) -> None:
        db, redis, storage = mock_clients
        db.check_connectivity.return_value = True
        redis.check_connectivity.return_value = False
        storage.check_connectivity.return_value = True

        service = ReadinessService(db, redis, storage)
        result = await service.check_readiness()

        assert result.status == OverallStatus.NOT_READY
        assert result.dependencies["redis"].status == DependencyStatus.FAILED

    @pytest.mark.asyncio
    async def test_storage_fails(self, mock_clients: tuple) -> None:
        db, redis, storage = mock_clients
        db.check_connectivity.return_value = True
        redis.check_connectivity.return_value = True
        storage.check_connectivity.return_value = False

        service = ReadinessService(db, redis, storage)
        result = await service.check_readiness()

        assert result.status == OverallStatus.NOT_READY
        assert result.dependencies["object_storage"].status == DependencyStatus.FAILED

    @pytest.mark.asyncio
    async def test_multiple_failures(self, mock_clients: tuple) -> None:
        db, redis, storage = mock_clients
        db.check_connectivity.return_value = False
        redis.check_connectivity.return_value = False
        storage.check_connectivity.return_value = False

        service = ReadinessService(db, redis, storage)
        result = await service.check_readiness()

        assert result.status == OverallStatus.NOT_READY
        for dep in result.dependencies.values():
            assert dep.status == DependencyStatus.FAILED

    @pytest.mark.asyncio
    async def test_exception_maps_to_failed(self, mock_clients: tuple) -> None:
        db, redis, storage = mock_clients
        db.check_connectivity.side_effect = Exception("DB crash")
        redis.check_connectivity.return_value = True
        storage.check_connectivity.return_value = True

        service = ReadinessService(db, redis, storage)
        result = await service.check_readiness()

        assert result.status == OverallStatus.NOT_READY
        assert result.dependencies["postgres"].status == DependencyStatus.FAILED
