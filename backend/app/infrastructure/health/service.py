"""Readiness service — orchestrates dependency health checks concurrently."""

from __future__ import annotations

import asyncio
import logging

from backend.app.infrastructure.database.client import DatabaseClient
from backend.app.infrastructure.health.checks import run_check
from backend.app.infrastructure.health.models import (
    DependencyResult,
    DependencyStatus,
    OverallStatus,
    ReadinessResponse,
)
from backend.app.infrastructure.redis.client import RedisClient
from backend.app.infrastructure.storage.client import StorageClient
from backend.app.infrastructure.storage.protocol import StoragePort

logger = logging.getLogger(__name__)


class ReadinessService:
    """Orchestrates concurrent readiness checks and aggregates results."""

    def __init__(
        self,
        database: DatabaseClient,
        redis: RedisClient,
        storage: StoragePort | StorageClient,
    ) -> None:
        self._database = database
        self._redis = redis
        self._storage = storage

    async def check_readiness(self) -> ReadinessResponse:
        """Execute all dependency checks concurrently and aggregate results.

        Returns a ReadinessResponse with per-dependency status and overall status.
        """
        results: dict[str, DependencyResult] = {}

        # Run checks concurrently with bounded timeouts
        db_coro = run_check("postgres", self._database.check_connectivity)
        redis_coro = run_check("redis", self._redis.check_connectivity)
        storage_coro = run_check("object_storage", self._storage.check_connectivity)

        db_status, redis_status, storage_status = await asyncio.gather(
            db_coro,
            redis_coro,
            storage_coro,
        )

        results["postgres"] = DependencyResult(status=db_status)
        results["redis"] = DependencyResult(status=redis_status)
        results["object_storage"] = DependencyResult(status=storage_status)

        all_ok = all(r.status == DependencyStatus.OK for r in results.values())

        overall = OverallStatus.READY if all_ok else OverallStatus.NOT_READY

        return ReadinessResponse(status=overall, dependencies=results)
