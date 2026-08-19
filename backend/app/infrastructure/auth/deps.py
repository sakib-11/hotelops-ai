"""FastAPI dependencies for authentication.

Extracts and validates Bearer token from Authorization header,
verifies JWT signature/expiry, resolves the user from the
database, and rejects disabled users.

Returns the verified TokenData — NOT an ActorContext.
Authorization (tenant/role/permissions) is resolved separately.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.app.dependencies import get_settings
from backend.app.infrastructure.auth.context import ActorContextBuilder
from backend.app.infrastructure.auth.exceptions import (
    AuthenticationError,
    AuthorizationError,
)
from backend.app.infrastructure.auth.service import AuthService, TokenData
from backend.app.infrastructure.config import Settings
from backend.app.infrastructure.observability import tracing
from backend.app.infrastructure.observability.context import (
    bind_actor_context,
    unbind,
)
from backend.app.infrastructure.observability.context import (
    venue_id as current_venue_id,
)
from contracts.identity import ActorContext, Permission

logger = logging.getLogger(__name__)

# FastAPI security scheme for Swagger UI
_security_scheme = HTTPBearer(auto_error=False)

# Type alias for user lookup callable — returns dict with at least 'user_id' and 'status'
UserLookup = Callable[[str], dict[str, Any] | None]


async def get_token_data(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security_scheme),
    settings: Settings = Depends(get_settings),
) -> TokenData:
    """Extract and verify Bearer token from Authorization header.

    This is the primary authentication dependency. It:
    1. Extracts the Bearer token from the Authorization header
    2. Verifies JWT signature, expiry, and issuer
    3. Returns verified TokenData (user_id only)

    Returns:
        TokenData with verified user_id and timestamps.

    Raises:
        AuthenticationError (→ 401) if:
        - No Authorization header
        - Not a Bearer token
        - Invalid/malformed/expired/tampered token

    Note:
        This does NOT resolve the user from the database.
        User resolution is handled by get_current_user().
    """
    if credentials is None:
        raise AuthenticationError("Missing Authorization header")

    if credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Invalid authentication scheme — must be Bearer")

    token = credentials.credentials
    if not token:
        raise AuthenticationError("Empty token")

    auth_service = AuthService(settings)
    return auth_service.verify(token)


async def get_current_user(
    token_data: TokenData = Depends(get_token_data),
    lookup: UserLookup | None = None,
) -> TokenData:
    """Resolve the authenticated user.

    Verifies the user exists in the system and is active.
    In production, the lookup callable is provided by the dependency
    injection layer. In tests, a mock lookup can be injected.

    Args:
        token_data: Verified token data from get_token_data.
        lookup: Optional callable that resolves user_id to user dict.
            When None (default for production), user lookup is skipped
            until the user repository is available.

    Returns:
        TokenData for the authenticated user.

    Raises:
        AuthenticationError (→ 401) if user is unknown or disabled.
    """
    if lookup is not None:
        user = lookup(token_data.user_id)
        if user is None:
            raise AuthenticationError("User not found")
        user_status: str | None = user.get("status")
        if user_status and user_status != "active":
            raise AuthenticationError("User account is disabled")

    return token_data


# =============================================================================
# RBAC Authorization Dependencies
# =============================================================================


def require_permission(permission: Permission) -> Callable[..., Any]:
    """FastAPI dependency factory: require a specific permission.

    Usage:
        @router.get("/analytics")
        async def get_analytics(
            _: None = Depends(require_permission(Permission.ANALYTICS_READ)),
        ):
            ...

    Raises:
        AuthorizationError (→ 403) if the actor lacks the required permission.
    """

    async def _check_permission(actor: ActorContext = Depends(get_actor_context)) -> None:
        if not actor.has_permission(permission):
            msg = f"Missing required permission: {permission.value}"
            raise AuthorizationError(msg)

    return _check_permission


def require_any_permission(*permissions: Permission) -> Callable[..., Any]:
    """FastAPI dependency factory: require at least one of the listed permissions.

    Usage:
        @router.get("/admin-panel")
        async def admin_panel(
            _: None = Depends(require_any_permission(
                Permission.USER_MANAGE,
                Permission.MEMBERSHIP_MANAGE,
            )),
        ):
            ...

    Raises:
        AuthorizationError (→ 403) if the actor lacks all of the listed permissions.
    """

    async def _check_any_permission(actor: ActorContext = Depends(get_actor_context)) -> None:
        if not permissions:
            raise ValueError("At least one permission required")
        for perm in permissions:
            if actor.has_permission(perm):
                return
        perm_names = ", ".join(p.value for p in permissions)
        msg = f"Missing required permission — need at least one of: {perm_names}"
        raise AuthorizationError(msg)

    return _check_any_permission


async def get_actor_context(
    token_data: TokenData = Depends(get_token_data),
) -> AsyncIterator[ActorContext]:
    """Resolve the ActorContext from a verified token (Task 5).

    Generator dependency: while the request is being served, the
    server-validated actor identity (actor_id, tenant_id, and the
    venue when unambiguous) is bound to the task-local observability
    context so structured logs carry it automatically (Task 8.5). The
    context is ALWAYS unbound when the request finishes — even on
    exceptions — so tenant/actor context can never leak into the next
    request.

    In production, this would inject real lookup callables from
    the repository layer. Currently returns a minimal ActorContext
    with default operator role when no lookups are configured.
    """
    builder = ActorContextBuilder()
    actor = builder.build(token_data)
    tokens = bind_actor_context(actor)
    # Attach the server-validated identity to the active span (Task 8.7):
    # actor/tenant/venue are trusted values only, and the venue is set
    # only when unambiguous (same rule as bind_actor_context).
    tracing.set_current_span_attributes(
        actor_id=str(actor.actor_id),
        tenant_id=str(actor.tenant_id),
        venue_id=current_venue_id(),
    )
    try:
        yield actor
    finally:
        unbind(tokens)
