"""JWT authentication service.

The token carries ONLY the user_id (sub claim).
All authorization state (tenant, role, permissions, venue scope)
is resolved SERVER-SIDE from database state.

Never trust client-provided tenant_id, role, or permissions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from backend.app.infrastructure.auth.exceptions import AuthenticationError
from backend.app.infrastructure.config import Settings

# Standard JWT claims
ISS = "iss"
ISS_VAL = "hotelops-ai"
SUB = "sub"
IAT = "iat"
EXP = "exp"


class TokenData:
    """Verified token payload.

    Contains ONLY identity claims extracted from the token.
    Authorization claims are NEVER trusted from the client.
    """

    def __init__(self, user_id: str, issued_at: datetime, expires_at: datetime) -> None:
        self.user_id = user_id
        self.issued_at = issued_at
        self.expires_at = expires_at

    def __repr__(self) -> str:
        return f"TokenData(user_id={self.user_id!r})"


def create_access_token(
    user_id: str,
    settings: Settings,
    *,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Create a signed JWT access token.

    The token encodes ONLY the user_id as the 'sub' claim.
    No authorization claims (tenant, role, permissions) are
    embedded in the token — those are resolved server-side.

    Args:
        user_id: Unique identifier for the user (UUID string).
        settings: Application settings with JWT configuration.
        extra_claims: Optional additional non-authorization claims.

    Returns:
        Signed JWT string.

    Raises:
        AuthenticationError: If JWT configuration is invalid.
    """
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=settings.jwt_expiration_minutes)

    payload: dict[str, Any] = {
        ISS: ISS_VAL,
        SUB: user_id,
        IAT: now,
        EXP: expires,
    }
    if extra_claims:
        # Only allow non-authorization claims
        blocked = {"tenant_id", "role", "permissions", "venue_scope", "is_admin"}
        for key in extra_claims:
            if key in blocked:
                msg = f"Cannot embed authorization claim in token: {key}"
                raise AuthenticationError(msg)
        payload.update(extra_claims)

    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def verify_token(token: str, settings: Settings) -> TokenData:
    """Verify and decode a JWT access token.

    Validates signature, expiration, issuer, and extracts
    ONLY the user_id for server-side user resolution.

    Args:
        token: The JWT string to verify.
        settings: Application settings with JWT configuration.

    Returns:
        TokenData containing the verified user_id and timestamps.

    Raises:
        AuthenticationError: If the token is invalid, expired,
            malformed, or tampered with.
    """
    if not token:
        raise AuthenticationError("Missing token")

    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.jwt_algorithm],
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "require": [SUB, EXP, IAT],
            },
        )
    except jwt.ExpiredSignatureError:
        raise AuthenticationError("Token expired") from None
    except jwt.InvalidSignatureError:
        raise AuthenticationError("Invalid token signature") from None
    except jwt.DecodeError:
        raise AuthenticationError("Invalid token format") from None
    except jwt.InvalidTokenError as e:
        raise AuthenticationError(f"Invalid token: {e}") from None

    user_id: str | None = payload.get(SUB)
    if not user_id:
        raise AuthenticationError("Token missing subject")

    # Extract timestamps with safe defaults
    iat_ts: int | None = payload.get(IAT)
    exp_ts: int | None = payload.get(EXP)

    issued_at = datetime.fromtimestamp(iat_ts, tz=UTC) if iat_ts else datetime.now(UTC)
    expires_at = datetime.fromtimestamp(exp_ts, tz=UTC) if exp_ts else datetime.now(UTC)

    return TokenData(user_id=user_id, issued_at=issued_at, expires_at=expires_at)


class AuthService:
    """Authentication service.

    Handles JWT token lifecycle and credential verification.
    Does NOT perform user resolution — that is handled by
    the FastAPI dependency layer.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def create_token(self, user_id: str) -> str:
        """Create an access token for a given user ID."""
        return create_access_token(user_id, self._settings)

    def verify(self, token: str) -> TokenData:
        """Verify and decode a token."""
        return verify_token(token, self._settings)
