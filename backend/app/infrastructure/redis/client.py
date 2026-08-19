"""Redis infrastructure boundary.

Async Redis client managed through the application lifespan.
No Redis Streams, consumer groups, or caching strategy in this module.
"""

from __future__ import annotations

import logging
from typing import Self

import redis.asyncio as aioredis

from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.observability import tracing

logger = logging.getLogger(__name__)


class RedisClient:
    """Redis infrastructure client.

    Manages the async Redis connection lifecycle.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: aioredis.Redis | None = None

    async def initialize(self) -> Self:
        """Create the Redis connection pool.

        Call during application startup.
        """
        if self._client is not None:
            logger.warning("Redis client already initialized")
            return self

        self._client = aioredis.Redis.from_url(
            self._settings.redis_url,
            socket_connect_timeout=5,
            socket_timeout=5,
            decode_responses=True,
        )
        logger.info("Redis client initialized")
        return self

    @property
    def client(self) -> aioredis.Redis:
        """The underlying async Redis connection.

        Lets higher layers (e.g. the Redis stream transport) reuse this
        client's connection lifecycle instead of opening a second one.

        Raises:
            RuntimeError: If the client has not been initialized.
        """
        if self._client is None:
            msg = "RedisClient is not initialized"
            raise RuntimeError(msg)
        return self._client

    async def check_connectivity(self) -> bool:
        """Check if Redis is reachable via PING.

        Returns True if PONG, False otherwise.
        """
        if self._client is None:
            return False
        async with tracing.redis_span("redis.check_connectivity") as _:
            try:
                result = await self._client.ping()
                return result is True
            except Exception:
                logger.exception("Redis connectivity check failed")
                return False

    async def close(self) -> None:
        """Close the Redis connection.

        Call during application shutdown.
        """
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("Redis client closed")
