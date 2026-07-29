"""PostgreSQL Row-Level Security context management.

Sets the `app.tenant_id` session parameter using `SET LOCAL` so that
RLS policies can enforce tenant isolation at the database level.

SET LOCAL is transaction-scoped — it is automatically cleared when
the transaction ends (commit/rollback), preventing context leakage
across pooled connections.

Architecture:
    Application Authorization
            ↓
    Repository Scope (WHERE tenant_id = :actor_tenant)
            ↓
    PostgreSQL RLS (policy via app.tenant_id)

Usage:
    await set_rls_on_session(session, tenant_id)
    # All queries on this session are now RLS-scoped
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

logger = logging.getLogger(__name__)


async def set_session_tenant(
    connection: AsyncConnection,
    tenant_id: UUID | str,
) -> None:
    """Set the tenant context for the current transaction using SET LOCAL.

    This must be called within an active transaction. The setting is
    automatically cleared on commit/rollback.

    Args:
        connection: An active database connection.
        tenant_id: The tenant UUID to set as the security context.
    """
    tid = str(tenant_id) if isinstance(tenant_id, UUID) else tenant_id
    # SET LOCAL is a PostgreSQL utility command and does not support
    # parameterized queries ($1/:param). UUID values are safe for
    # string formatting since they are validated by the contract types.
    await connection.execute(text(f"SET LOCAL app.tenant_id = '{tid}'"))
    logger.debug("RLS context set for tenant: %s", tid)


async def clear_session_tenant(connection: AsyncConnection) -> None:
    """Clear the tenant context for the current transaction.

    Uses RESET to remove the parameter entirely, so that
    current_setting('app.tenant_id', true) returns NULL,
    triggering the fail-closed fallback in RLS policies.
    """
    await connection.execute(text("RESET app.tenant_id"))
    logger.debug("RLS context cleared")


async def set_rls_on_session(
    session: AsyncSession,
    tenant_id: UUID | str,
) -> None:
    """Set RLS tenant context directly on an existing session.

    The setting persists until the current transaction ends.
    Uses SET LOCAL which is transaction-scoped — automatically
    cleared on commit/rollback, preventing context leakage
    across pooled connections.

    Args:
        session: An active async SQLAlchemy session.
        tenant_id: The tenant UUID to scope queries to.
    """
    conn = await session.connection()
    await set_session_tenant(conn, tenant_id)


async def clear_rls_on_session(session: AsyncSession) -> None:
    """Clear RLS tenant context on a session.

    Uses RESET to remove the app.tenant_id parameter, causing
    current_setting('app.tenant_id', true) to return NULL,
    which triggers the RLS policy fail-closed fallback.
    """
    conn = await session.connection()
    await clear_session_tenant(conn)
