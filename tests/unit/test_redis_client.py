"""Unit tests for RedisClient.

These tests use mocks/stubs, not a real Redis instance.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.redis.client import RedisClient


class TestRedisClient:
    """Tests for RedisClient."""

    @pytest.mark.asyncio
    async def test_initialize_and_close(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        client = RedisClient(settings)

        assert client._client is None

        with patch("redis.asyncio.Redis.from_url") as mock_from_url:
            mock_redis = AsyncMock()
            mock_from_url.return_value = mock_redis

            await client.initialize()
            assert client._client is not None
            mock_from_url.assert_called_once()

            await client.close()
            mock_redis.aclose.assert_called_once()
            assert client._client is None

    @pytest.mark.asyncio
    async def test_check_connectivity_when_not_initialized(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        client = RedisClient(settings)

        result = await client.check_connectivity()
        assert result is False

    @pytest.mark.asyncio
    async def test_check_connectivity_success(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        client = RedisClient(settings)

        with patch("redis.asyncio.Redis.from_url") as mock_from_url:
            mock_redis = AsyncMock()
            mock_redis.ping.return_value = True
            mock_from_url.return_value = mock_redis

            await client.initialize()
            result = await client.check_connectivity()
            assert result is True
            mock_redis.ping.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_connectivity_failure(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        client = RedisClient(settings)

        with patch("redis.asyncio.Redis.from_url") as mock_from_url:
            mock_redis = AsyncMock()
            mock_redis.ping.side_effect = Exception("connection refused")
            mock_from_url.return_value = mock_redis

            await client.initialize()
            result = await client.check_connectivity()
            assert result is False
