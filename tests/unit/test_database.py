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

    @pytest.mark.asyncio
    async def test_session_factory_requires_initialization(self) -> None:
        """The session factory is only available after initialize()."""
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        client = DatabaseClient(settings)

        with pytest.raises(RuntimeError, match="not initialized"):
            _ = client.session_factory


class _FakeSessionContext:
    """Minimal async context manager standing in for `async with factory() as s:`."""

    def __init__(self, session: AsyncMock) -> None:
        self._session = session
        self.exited = False

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.exited = True
        return False


def _client_with_factory(
    sessions: list[AsyncMock],
) -> tuple[DatabaseClient, list[_FakeSessionContext]]:
    """DatabaseClient backed by a mock session factory yielding the given sessions."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    client = DatabaseClient(settings)
    contexts: list[_FakeSessionContext] = []

    def _make_context() -> _FakeSessionContext:
        context = _FakeSessionContext(sessions[len(contexts)])
        contexts.append(context)
        return context

    factory = MagicMock()
    factory.side_effect = _make_context
    client._session_factory = factory  # type: ignore[assignment]
    return client, contexts


class TestDatabaseSessionLifecycle:
    """Task 6.2 — transaction-scoped session unit of work.

    Session lifecycle: create -> commit on success / rollback on failure
    -> always close. A broken session is never reused.
    """

    @pytest.mark.asyncio
    async def test_session_requires_initialized_client(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        client = DatabaseClient(settings)

        with pytest.raises(RuntimeError, match="not initialized"):
            async with client.session():
                pass  # pragma: no cover

    @pytest.mark.asyncio
    async def test_session_commits_on_success(self) -> None:
        session = AsyncMock()
        client, contexts = _client_with_factory([session])

        async with client.session() as yielded:
            assert yielded is session

        session.commit.assert_awaited_once()
        session.rollback.assert_not_called()
        assert contexts[0].exited is True, "Session must be closed after use"

    @pytest.mark.asyncio
    async def test_session_rolls_back_on_exception(self) -> None:
        session = AsyncMock()
        client, contexts = _client_with_factory([session])

        with pytest.raises(RuntimeError, match="boom"):
            async with client.session():
                raise RuntimeError("boom")

        session.rollback.assert_awaited_once()
        session.commit.assert_not_called()
        assert contexts[0].exited is True, "Session must be closed after failure"

    @pytest.mark.asyncio
    async def test_rollback_failure_preserves_original_error(self) -> None:
        """A failing rollback must not mask the original error."""
        session = AsyncMock()
        session.rollback.side_effect = RuntimeError("rollback failed too")
        client, _ = _client_with_factory([session])

        with pytest.raises(RuntimeError, match="boom"):
            async with client.session():
                raise RuntimeError("boom")

    @pytest.mark.asyncio
    async def test_session_is_fresh_per_call(self) -> None:
        """Each session() call yields a new session — broken ones are never reused."""
        first, second = AsyncMock(), AsyncMock()
        client, _ = _client_with_factory([first, second])

        async with client.session() as a:
            pass
        async with client.session() as b:
            pass

        assert a is not b
        first.commit.assert_awaited_once()
        second.commit.assert_awaited_once()
