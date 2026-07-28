"""Unit tests for DatabaseClient.

These tests use mocks/stubs, not a real database.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.database.client import DatabaseClient


class TestDatabaseClient:
    """Tests for DatabaseClient."""

    @pytest.mark.asyncio
    async def test_initialize_and_dispose(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        client = DatabaseClient(settings)

        assert client._engine is None

        with patch("backend.app.infrastructure.database.client.create_async_engine") as mock_create:
            mock_engine = AsyncMock()
            mock_engine.dispose = AsyncMock()
            mock_create.return_value = mock_engine

            await client.initialize()
            assert client._engine is not None
            mock_create.assert_called_once()

            await client.dispose()
            mock_engine.dispose.assert_awaited_once()
            assert client._engine is None

    @pytest.mark.asyncio
    async def test_check_connectivity_when_not_initialized(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        client = DatabaseClient(settings)

        result = await client.check_connectivity()
        assert result is False

    @pytest.mark.asyncio
    async def test_check_connectivity_success(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        client = DatabaseClient(settings)

        with patch("backend.app.infrastructure.database.client.create_async_engine") as mock_create:
            mock_engine = MagicMock()
            # Mock async context manager for engine.connect()
            mock_connection = AsyncMock()
            mock_engine.connect.return_value.__aenter__.return_value = mock_connection
            # Mock execute returning SELECT 1 = 1
            mock_result = MagicMock()
            mock_result.scalar_one.return_value = 1
            mock_connection.execute.return_value = mock_result
            mock_create.return_value = mock_engine

            await client.initialize()
            result = await client.check_connectivity()
            assert result is True
            mock_connection.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_connectivity_failure(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        client = DatabaseClient(settings)

        with patch("backend.app.infrastructure.database.client.create_async_engine") as mock_create:
            mock_engine = MagicMock()
            mock_connection = AsyncMock()
            mock_connection.execute.side_effect = Exception("connection failed")
            mock_engine.connect.return_value.__aenter__.return_value = mock_connection
            mock_create.return_value = mock_engine

            await client.initialize()
            result = await client.check_connectivity()
            assert result is False
