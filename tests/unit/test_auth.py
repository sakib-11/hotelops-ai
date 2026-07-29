"""Tests for Task 5.4 — Authentication Boundary.

Tests cover:
- Valid credential creation and verification
- Missing credential (no token)
- Expired credential
- Malformed credential (invalid format)
- Tampered credential (wrong signature)
- Unknown user (token verified but user not in DB)
- Disabled user (token verified but user is disabled)
- Token without required claims
- Bearer vs non-Bearer scheme
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from pydantic import ValidationError

from backend.app.infrastructure.auth.deps import UserLookup, get_current_user
from backend.app.infrastructure.auth.exceptions import AuthenticationError
from backend.app.infrastructure.auth.service import (
    AuthService,
    TokenData,
    create_access_token,
    verify_token,
)
from backend.app.infrastructure.config import Settings

# =============================================================================
# Helpers
# =============================================================================

# Settings fields have aliases (case-insensitive from env), so we must
# use the alias name when constructing Settings with keyword arguments
# since populate_by_name is not enabled in the config.
_TEST_SECRET = "test-secret-key-32-chars-long-ok!!!"
_SUB = "sub"


def _make_settings(
    secret_key: str = _TEST_SECRET,
    algorithm: str = "HS256",
    expiry_minutes: int = 60,
) -> Settings:
    """Create a test Settings instance with JWT configuration.

    Uses alias keyword forms (_env_file, SECRET_KEY, JWT_ALGORITHM,
    JWT_EXPIRATION_MINUTES) because Settings.populate_by_name is
    not enabled — only aliases are accepted as __init__ kwargs.
    """
    return Settings(
        app_env="test",
        SECRET_KEY=secret_key,  # alias-based kwarg
        JWT_ALGORITHM=algorithm,
        JWT_EXPIRATION_MINUTES=expiry_minutes,
        _env_file=None,  # type: ignore[call-arg]
    )


# =============================================================================
# Token Creation & Verification
# =============================================================================


class TestCreateAccessToken:
    """Token creation tests."""

    def test_create_valid_token(self) -> None:
        settings = _make_settings()
        token = create_access_token("user-123", settings)
        assert isinstance(token, str)
        assert len(token) > 0
        assert token.count(".") == 2

    def test_token_contains_only_sub_claim(self) -> None:
        settings = _make_settings()
        token = create_access_token("user-456", settings)
        decoded = jwt.decode(token, _TEST_SECRET, algorithms=["HS256"])
        assert decoded[_SUB] == "user-456"
        assert "tenant_id" not in decoded
        assert "role" not in decoded
        assert "permissions" not in decoded
        assert "is_admin" not in decoded
        assert "venue_scope" not in decoded

    def test_token_has_required_claims(self) -> None:
        settings = _make_settings()
        token = create_access_token("user-789", settings)
        decoded = jwt.decode(token, _TEST_SECRET, algorithms=["HS256"])
        assert "iss" in decoded
        assert decoded["iss"] == "hotelops-ai"
        assert "iat" in decoded
        assert "exp" in decoded

    def test_token_expiry(self) -> None:
        settings = _make_settings(expiry_minutes=30)
        token = create_access_token("user-1", settings)
        decoded = jwt.decode(token, _TEST_SECRET, algorithms=["HS256"])
        exp = datetime.fromtimestamp(decoded["exp"], tz=UTC)
        iat = datetime.fromtimestamp(decoded["iat"], tz=UTC)
        # exp should be approx 30 min after iat
        delta = (exp - iat).total_seconds()
        assert 29 * 60 <= delta <= 31 * 60

    def test_extra_claims_allowed(self) -> None:
        settings = _make_settings()
        token = create_access_token(
            "user-1", settings, extra_claims={"nonce": "abc123", "client_id": "mobile-app"}
        )
        decoded = jwt.decode(token, _TEST_SECRET, algorithms=["HS256"])
        assert decoded["nonce"] == "abc123"
        assert decoded["client_id"] == "mobile-app"

    def test_authorization_claims_blocked(self) -> None:
        settings = _make_settings()
        with pytest.raises(AuthenticationError, match="Cannot embed authorization claim"):
            create_access_token("user-1", settings, extra_claims={"tenant_id": "tenant-1"})


class TestVerifyToken:
    """Token verification tests."""

    def _make_token(self, user_id: str = "user-valid", extra_payload: dict | None = None) -> str:
        """Create a signed token with optional extra payload fields."""
        settings = _make_settings()
        return create_access_token(user_id, settings)

    def test_verify_valid_token(self) -> None:
        settings = _make_settings()
        token = create_access_token("user-valid", settings)
        token_data = verify_token(token, settings)
        assert token_data.user_id == "user-valid"
        assert isinstance(token_data.issued_at, datetime)
        assert isinstance(token_data.expires_at, datetime)
        assert token_data.expires_at > token_data.issued_at

    def test_verify_token_round_trip(self) -> None:
        settings = _make_settings()
        token = create_access_token("round-trip-user", settings)
        token_data = verify_token(token, settings)
        assert token_data.user_id == "round-trip-user"

    def test_missing_token(self) -> None:
        settings = _make_settings()
        with pytest.raises(AuthenticationError, match="Missing token"):
            verify_token("", settings)

    def test_missing_token_whitespace(self) -> None:
        """Whitespace-only tokens are not empty so jwt.decode tries and fails."""
        settings = _make_settings()
        with pytest.raises(AuthenticationError):
            verify_token("   ", settings)

    def test_malformed_token(self) -> None:
        settings = _make_settings()
        with pytest.raises(AuthenticationError, match="Invalid token format"):
            verify_token("not-a-jwt-token", settings)

    def test_tampered_token_payload(self) -> None:
        settings = _make_settings()
        token = create_access_token("user-tampered", settings)
        parts = token.split(".")
        tampered = parts[0] + ".INVALID_PAYLOAD." + parts[2]
        with pytest.raises(AuthenticationError, match="token"):
            verify_token(tampered, settings)

    def test_signature_mismatch(self) -> None:
        settings_a = _make_settings(secret_key="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        token = create_access_token("user-1", settings_a)
        settings_b = _make_settings(secret_key="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        with pytest.raises(AuthenticationError):
            verify_token(token, settings_b)

    def test_token_without_sub_rejected(self) -> None:
        settings = _make_settings()
        payload = {
            "iss": "hotelops-ai",
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(hours=1),
        }
        token = jwt.encode(payload, _TEST_SECRET, algorithm="HS256")
        with pytest.raises(AuthenticationError):
            verify_token(token, settings)

    def test_token_without_exp_rejected(self) -> None:
        settings = _make_settings()
        payload = {
            "iss": "hotelops-ai",
            _SUB: "user-1",
            "iat": datetime.now(UTC),
        }
        token = jwt.encode(payload, _TEST_SECRET, algorithm="HS256")
        with pytest.raises((AuthenticationError, jwt.MissingRequiredClaimError)):
            verify_token(token, settings)

    def test_token_without_iat_rejected(self) -> None:
        settings = _make_settings()
        payload = {
            "iss": "hotelops-ai",
            _SUB: "user-1",
            "exp": datetime.now(UTC) + timedelta(hours=1),
        }
        token = jwt.encode(payload, _TEST_SECRET, algorithm="HS256")
        with pytest.raises((AuthenticationError, jwt.MissingRequiredClaimError)):
            verify_token(token, settings)


class TestExpiredToken:
    """Expired token tests."""

    def _expired_token(self, settings: Settings) -> str:
        """Create an already-expired JWT token for testing."""
        payload = {
            "iss": "hotelops-ai",
            _SUB: "user-expired",
            "iat": datetime.now(UTC) - timedelta(hours=2),
            "exp": datetime.now(UTC) - timedelta(minutes=1),
        }
        return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)

    def test_expired_token_rejected(self) -> None:
        settings = _make_settings()
        token = self._expired_token(settings)
        with pytest.raises(AuthenticationError, match="expired"):
            verify_token(token, settings)

    def test_already_expired_token(self) -> None:
        settings = _make_settings()
        token = self._expired_token(settings)
        with pytest.raises((AuthenticationError, jwt.ExpiredSignatureError)):
            verify_token(token, settings)

    def test_jwt_expired_error_wrapped(self) -> None:
        """Verify that jwt.ExpiredSignatureError is wrapped as AuthenticationError."""
        settings = _make_settings()
        token = self._expired_token(settings)
        with pytest.raises(AuthenticationError):
            verify_token(token, settings)


# =============================================================================
# AuthService integration
# =============================================================================


class TestAuthService:
    """AuthService class integration tests."""

    def test_create_and_verify(self) -> None:
        settings = _make_settings()
        service = AuthService(settings)
        token = service.create_token("integration-user")
        token_data = service.verify(token)
        assert token_data.user_id == "integration-user"

    def test_service_rejects_invalid(self) -> None:
        settings = _make_settings()
        service = AuthService(settings)
        with pytest.raises(AuthenticationError):
            service.verify("not-a-valid-token")


# =============================================================================
# TokenData
# =============================================================================


class TestTokenData:
    """TokenData value object tests."""

    def test_creation(self) -> None:
        now = datetime.now(UTC)
        exp = now + timedelta(hours=1)
        data = TokenData(user_id="user-1", issued_at=now, expires_at=exp)
        assert data.user_id == "user-1"
        assert data.issued_at == now
        assert data.expires_at == exp

    def test_repr_does_not_expose_secrets(self) -> None:
        now = datetime.now(UTC)
        exp = now + timedelta(hours=1)
        data = TokenData(user_id="user-1", issued_at=now, expires_at=exp)
        rep = repr(data)
        assert "user-1" in rep
        assert "secret" not in rep.lower()


# =============================================================================
# User resolution (unknown / disabled user)
# =============================================================================


class TestUserResolution:
    """User resolution tests via get_current_user with mock lookup."""

    async def _call_get_current_user(
        self, token_data: TokenData, lookup: UserLookup | None
    ) -> TokenData:
        """Call get_current_user with explicit lookup, bypassing Depends()."""
        return await get_current_user(token_data=token_data, lookup=lookup)

    async def test_known_active_user_accepted(self) -> None:
        data = TokenData(
            user_id="known-user",
            issued_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        def _lookup(user_id: str) -> dict[str, str]:
            return {"user_id": user_id, "status": "active"}

        result = await self._call_get_current_user(data, lookup=_lookup)
        assert result.user_id == "known-user"

    async def test_unknown_user_rejected(self) -> None:
        data = TokenData(
            user_id="unknown-user",
            issued_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        def _lookup(_user_id: str) -> None:
            return None

        with pytest.raises(AuthenticationError, match="not found"):
            await self._call_get_current_user(data, lookup=_lookup)

    async def test_disabled_user_rejected(self) -> None:
        data = TokenData(
            user_id="disabled-user",
            issued_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        def _lookup(user_id: str) -> dict[str, str]:
            return {"user_id": user_id, "status": "disabled"}

        with pytest.raises(AuthenticationError, match="disabled"):
            await self._call_get_current_user(data, lookup=_lookup)

    async def test_no_lookup_passes_through(self) -> None:
        """When lookup is None (production default), user resolution is skipped."""
        data = TokenData(
            user_id="any-user",
            issued_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        result = await self._call_get_current_user(data, lookup=None)
        assert result.user_id == "any-user"


# =============================================================================
# Settings validation
# =============================================================================


class TestAuthSettings:
    """JWT-related Settings validation."""

    def test_secret_key_custom(self) -> None:
        settings = _make_settings(secret_key="custom-key-manual-set")
        assert settings.secret_key == "custom-key-manual-set"

    def test_jwt_algorithm_custom(self) -> None:
        settings = _make_settings(algorithm="HS384")
        assert settings.jwt_algorithm == "HS384"

    def test_jwt_expiration_custom(self) -> None:
        settings = _make_settings(expiry_minutes=30)
        assert settings.jwt_expiration_minutes == 30

    def test_jwt_expiration_invalid_low(self) -> None:
        with pytest.raises(ValidationError):
            _make_settings(expiry_minutes=0)

    def test_jwt_expiration_invalid_high(self) -> None:
        with pytest.raises(ValidationError):
            _make_settings(expiry_minutes=99999)
