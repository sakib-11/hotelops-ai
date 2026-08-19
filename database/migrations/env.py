"""Alembic migration environment configuration.

Loads ORM models from the application (registering them on Base.metadata)
and configures the async database connection for migrations.

Database URL — single configuration system (no second config source):
    1. an explicit sqlalchemy.url in the config (e.g. set_main_option),
    2. the $DATABASE_URL environment variable,
    3. Settings().database_url (project configuration).

Migration can be invoked via:
    alembic -c database/alembic.ini upgrade head
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

# Import all models so they are registered with Base.metadata
import backend.app.infrastructure.database.models  # ruff: ignore[unused-import]
from backend.app.infrastructure.database.base import Base

# Alembic Config object
config = context.config

# Set up logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for auto-generation — points to our declarative base
target_metadata = Base.metadata


def resolve_database_url() -> str:
    """Resolve the database URL from the single configuration system.

    Precedence: explicit config value -> $DATABASE_URL -> Settings.
    """
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        return env_url
    from backend.app.infrastructure.config import Settings

    return Settings().database_url  # type: ignore[call-arg]  # pydantic default fields


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Configured URLs are passed directly. This is used for generating
    migration scripts without a live database connection.
    """
    context.configure(
        url=resolve_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Execute migration operations against a live connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' async mode.

    Creates an async engine from the resolved URL and runs
    all pending migrations.
    """
    connectable = create_async_engine(
        resolve_database_url(),
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode. Dispatches to async runner.

    asyncio.run() requires that no event loop is already running — this is
    the normal case for the `alembic` CLI. Programmatic invocation from a
    running loop (e.g. from tests) must wrap this in asyncio.to_thread().
    """
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
