"""Authentication boundary for HotelOps AI.

Authentication answers: WHO IS THIS?
Authorization answers: WHAT MAY THEY ACCESS?

JWT-based authentication with server-side user resolution.
The token carries only the user_id (sub); all authorization
state is resolved server-side.
"""

from backend.app.infrastructure.auth.context import ActorContextBuilder
from backend.app.infrastructure.auth.exceptions import AuthenticationError, AuthorizationError
from backend.app.infrastructure.auth.handler import (
    authentication_error_handler,
    authorization_error_handler,
)
from backend.app.infrastructure.auth.scope import (
    require_same_tenant,
    require_tenant_venue_access,
    require_venue_access,
)
from backend.app.infrastructure.auth.service import (
    AuthService,
    TokenData,
    create_access_token,
    verify_token,
)

__all__ = [
    "ActorContextBuilder",
    "AuthService",
    "AuthenticationError",
    "AuthorizationError",
    "TokenData",
    "authentication_error_handler",
    "authorization_error_handler",
    "create_access_token",
    "require_same_tenant",
    "require_tenant_venue_access",
    "require_venue_access",
    "verify_token",
]
