"""Shared fixtures for the Task 7 reliability integration tests.

Provides scratch migrated databases (real Alembic chain) and a real
Redis transport, following the scratch-database convention established
in tests/integration/test_migrations.py. Fixture names are prefixed
``task7_`` so they cannot collide with the inline fixtures of existing
integration test modules.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

from backend.app.infrastructure.config import Settings
from tests.integration._task7_helpers import (
    create_scratch_database,
    drop_database,
    make_redis_transport,
    scratch_settings,
    scratch_url,
    upgrade_to_head,
)

_requires_postgres = pytest.mark.skipif(
    not os.environ.get("INTEGRATION_TESTS"),
    reason="Set INTEGRATION_TESTS=1 and start PostgreSQL/Redis "
    "(docker compose -f infrastructure/docker/compose.yaml up -d)",
)


@pytest_asyncio.fixture
async def task7_db():
    """A unique scratch database migrated to the current head (016)."""
    name = await create_scratch_database()
    try:
        await upgrade_to_head(name)
        yield {"name": name, "url": scratch_url(name)}
    finally:
        await drop_database(name)


@pytest_asyncio.fixture
async def task7_redis():
    """A real Redis client + stream transport (skipped if Redis is down)."""
    settings = scratch_settings("hotelops", REDIS_STREAM_EVENTS="hotelops:events:test")
    try:
        redis, transport = await make_redis_transport(settings)
    except RuntimeError as exc:
        pytest.skip(f"Redis unavailable: {exc}")
    try:
        yield {"redis": redis, "transport": transport, "settings": settings}
    finally:
        await redis.close()


@pytest.fixture
def task7_settings(task7_db) -> Settings:
    """Settings pointing at a scratch database (worker defaults)."""
    return scratch_settings(task7_db["name"])
