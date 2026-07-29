"""Tests for Task 5.10 — Audit Identity Context.

Tests cover:
- AuditEvent creation from ActorContext
- All identity fields derive from ActorContext, never from client data
- Secrets/passwords/tokens are rejected from audit metadata
- Spoofed client data cannot alter audit identity
- Serialization round-trip
- Missing optional fields
- AuditActionCategory validation
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from backend.app.infrastructure.audit.context import AuditEventBuilder
from contracts.audit import AuditActionCategory, AuditEvent
from contracts.common import MembershipId, TenantId, UserId, VenueId
from contracts.identity import ActorContext, RoleName, permissions_for_role

# =============================================================================
# Helpers
# =============================================================================


def _uid() -> str:
    return str(uuid4())


def _make_actor(
    user_id: str | None = None,
    tenant_id: str | None = None,
    role_name: RoleName = RoleName.OPERATOR,
) -> ActorContext:
    """Create a test ActorContext with server-resolved state."""
    uid = UserId(UUID(user_id or _uid()))
    tid = TenantId(UUID(tenant_id or _uid()))
    return ActorContext(
        actor_id=uid,
        tenant_id=tid,
        role_name=role_name,
        permissions=permissions_for_role(role_name),
        authenticated_at=datetime.now(UTC),
        active=True,
    )


# =============================================================================
# AuditEvent Contract Tests
# =============================================================================


class TestAuditEventContract:
    """Tests for the AuditEvent Pydantic model itself."""

    def test_creation_basic(self) -> None:
        """A basic AuditEvent can be created with required fields."""
        actor = _make_actor()
        event = AuditEvent(
            actor_id=actor.actor_id,
            tenant_id=actor.tenant_id,
            action="venue.created",
            action_category=AuditActionCategory.VENUE,
        )
        assert event.actor_id == actor.actor_id
        assert event.tenant_id == actor.tenant_id
        assert event.action == "venue.created"
        assert event.action_category == AuditActionCategory.VENUE
        assert event.schema_version == "1.0"
        assert event.timestamp.tzinfo is not None
        assert event.membership_id is None
        assert event.venue_id is None
        assert event.correlation_id is None
        assert event.metadata is None

    def test_creation_with_all_fields(self) -> None:
        """An AuditEvent with all optional fields can be created."""
        actor = _make_actor()
        membership_id = MembershipId(uuid4())
        venue_id = VenueId(uuid4())
        ts = datetime.now(UTC)
        event = AuditEvent(
            actor_id=actor.actor_id,
            tenant_id=actor.tenant_id,
            action="user.login",
            action_category=AuditActionCategory.AUTHENTICATION,
            membership_id=membership_id,
            venue_id=venue_id,
            correlation_id="req-abc-123",
            timestamp=ts,
            metadata={"ip_address": "192.168.1.1", "user_agent": "Mozilla/5.0"},
        )
        assert event.membership_id == membership_id
        assert event.venue_id == venue_id
        assert event.correlation_id == "req-abc-123"
        assert event.timestamp == ts
        assert event.metadata == {"ip_address": "192.168.1.1", "user_agent": "Mozilla/5.0"}

    def test_frozen_immutable(self) -> None:
        """AuditEvent should be immutable after creation."""
        actor = _make_actor()
        event = AuditEvent(
            actor_id=actor.actor_id,
            tenant_id=actor.tenant_id,
            action="test",
            action_category=AuditActionCategory.SYSTEM,
        )
        with pytest.raises(ValidationError):
            event.actor_id = UserId(uuid4())  # type: ignore[misc]

    def test_unknown_fields_rejected(self) -> None:
        """Extra fields not in the model should be rejected."""
        actor = _make_actor()
        with pytest.raises(ValidationError):
            AuditEvent(
                actor_id=actor.actor_id,
                tenant_id=actor.tenant_id,
                action="test",
                action_category=AuditActionCategory.SYSTEM,
                unknown_field="should_not_exist",  # type: ignore[call-arg]
            )

    def test_empty_action_rejected(self) -> None:
        """Action must be a non-empty string."""
        actor = _make_actor()
        with pytest.raises(ValidationError):
            AuditEvent(
                actor_id=actor.actor_id,
                tenant_id=actor.tenant_id,
                action="",
                action_category=AuditActionCategory.SYSTEM,
            )

    def test_invalid_schema_version_rejected(self) -> None:
        """Non-1.0 schema version should be rejected."""
        actor = _make_actor()
        with pytest.raises(ValidationError):
            AuditEvent(
                actor_id=actor.actor_id,
                tenant_id=actor.tenant_id,
                action="test",
                action_category=AuditActionCategory.SYSTEM,
                schema_version="999.0",
            )

    def test_invalid_action_category_rejected(self) -> None:
        """Invalid action category strings should be rejected."""
        actor = _make_actor()
        with pytest.raises(ValidationError):
            AuditEvent(
                actor_id=actor.actor_id,
                tenant_id=actor.tenant_id,
                action="test",
                action_category="invalid_category",  # type: ignore[arg-type]
            )


# =============================================================================
# Secrets Rejection Tests
# =============================================================================


class TestSecretsExcluded:
    """Sensitive data must never appear in audit records."""

    def test_password_in_metadata_rejected(self) -> None:
        """Metadata key starting with 'password' is rejected."""
        actor = _make_actor()
        with pytest.raises(ValidationError, match="Never audit"):
            AuditEvent(
                actor_id=actor.actor_id,
                tenant_id=actor.tenant_id,
                action="test",
                action_category=AuditActionCategory.SYSTEM,
                metadata={"password_hash": "should_not_appear"},
            )

    def test_token_in_metadata_rejected(self) -> None:
        """Metadata key starting with 'token' is rejected."""
        actor = _make_actor()
        with pytest.raises(ValidationError, match="Never audit"):
            AuditEvent(
                actor_id=actor.actor_id,
                tenant_id=actor.tenant_id,
                action="test",
                action_category=AuditActionCategory.SYSTEM,
                metadata={"token": "should_not_appear"},
            )

    def test_secret_in_metadata_rejected(self) -> None:
        """Metadata key starting with 'secret' is rejected."""
        actor = _make_actor()
        with pytest.raises(ValidationError, match="Never audit"):
            AuditEvent(
                actor_id=actor.actor_id,
                tenant_id=actor.tenant_id,
                action="test",
                action_category=AuditActionCategory.SYSTEM,
                metadata={"secret_key": "should_not_appear"},
            )

    def test_key_in_metadata_rejected(self) -> None:
        """Metadata key with 'key' as first segment is rejected."""
        actor = _make_actor()
        with pytest.raises(ValidationError, match="Never audit"):
            AuditEvent(
                actor_id=actor.actor_id,
                tenant_id=actor.tenant_id,
                action="test",
                action_category=AuditActionCategory.SYSTEM,
                metadata={"key_data": "should_not_appear"},
            )

    def test_credential_in_metadata_rejected(self) -> None:
        """Metadata key starting with 'credential' is rejected."""
        actor = _make_actor()
        with pytest.raises(ValidationError, match="Never audit"):
            AuditEvent(
                actor_id=actor.actor_id,
                tenant_id=actor.tenant_id,
                action="test",
                action_category=AuditActionCategory.SYSTEM,
                metadata={"credential_data": "should_not_appear"},
            )

    def test_authorization_in_metadata_rejected(self) -> None:
        """Metadata key starting with 'authorization' is rejected."""
        actor = _make_actor()
        with pytest.raises(ValidationError, match="Never audit"):
            AuditEvent(
                actor_id=actor.actor_id,
                tenant_id=actor.tenant_id,
                action="test",
                action_category=AuditActionCategory.SYSTEM,
                metadata={"authorization_header": "should_not_appear"},
            )

    def test_safe_metadata_accepted(self) -> None:
        """Non-sensitive metadata keys are accepted."""
        actor = _make_actor()
        safe_meta = {"ip_address": "10.0.0.1", "user_agent": "curl/7.68", "browser": "Chrome"}
        event = AuditEvent(
            actor_id=actor.actor_id,
            tenant_id=actor.tenant_id,
            action="test",
            action_category=AuditActionCategory.SYSTEM,
            metadata=safe_meta,
        )
        assert event.metadata == safe_meta

    def test_case_insensitive_rejection(self) -> None:
        """Blocked prefixes are checked case-insensitively."""
        actor = _make_actor()
        with pytest.raises(ValidationError, match="Never audit"):
            AuditEvent(
                actor_id=actor.actor_id,
                tenant_id=actor.tenant_id,
                action="test",
                action_category=AuditActionCategory.SYSTEM,
                metadata={"PASSWORD_hash": "should_not_appear"},  # uppercase prefix
            )


# =============================================================================
# AuditEventBuilder Tests
# =============================================================================


class TestAuditEventBuilder:
    """AuditEventBuilder derives audit records from ActorContext."""

    def test_from_actor_basic(self) -> None:
        """from_actor derives actor_id and tenant_id from ActorContext."""
        actor = _make_actor()
        event = AuditEventBuilder.from_actor(
            actor=actor,
            action="venue.created",
            action_category=AuditActionCategory.VENUE,
        )
        assert event.actor_id == actor.actor_id
        assert event.tenant_id == actor.tenant_id
        assert event.action == "venue.created"
        assert event.action_category == AuditActionCategory.VENUE

    def test_from_actor_with_all_optionals(self) -> None:
        """from_actor passes all optional fields correctly."""
        actor = _make_actor()
        membership_id = MembershipId(uuid4())
        venue_id = VenueId(uuid4())
        correlation_id = "req-xyz"

        event = AuditEventBuilder.from_actor(
            actor=actor,
            action="video.analyzed",
            action_category=AuditActionCategory.VIDEO,
            correlation_id=correlation_id,
            venue_id=venue_id,
            membership_id=membership_id,
            metadata={"duration_seconds": "42"},
        )
        assert event.correlation_id == correlation_id
        assert event.venue_id == venue_id
        assert event.membership_id == membership_id
        assert event.metadata == {"duration_seconds": "42"}


# =============================================================================
# Cross-Tenant Spoofing Cannot Alter Audit Identity
# =============================================================================


class TestClientCannotAlterAuditIdentity:
    """Client-supplied values must never alter audit identity fields.

    The audit identity (actor_id, tenant_id) is derived from the
    server-built ActorContext. A client cannot forge audit records
    by providing different identity values.
    """

    def test_actor_id_from_actor_not_client(self) -> None:
        """The audit actor_id comes from ActorContext, never client input."""
        real_uid = _uid()
        fake_uid = _uid()

        # Build ActorContext with the REAL user
        actor = _make_actor(user_id=real_uid)

        # Build AuditEvent from that actor — client cannot override actor_id
        event = AuditEventBuilder.from_actor(
            actor=actor,
            action="test",
            action_category=AuditActionCategory.SYSTEM,
        )
        assert str(event.actor_id) == real_uid
        assert str(event.actor_id) != fake_uid

    def test_tenant_id_from_actor_not_client(self) -> None:
        """The audit tenant_id comes from ActorContext, never client input."""
        real_tid = _uid()
        fake_tid = _uid()

        actor = _make_actor(tenant_id=real_tid)

        event = AuditEventBuilder.from_actor(
            actor=actor,
            action="test",
            action_category=AuditActionCategory.SYSTEM,
        )
        assert str(event.tenant_id) == real_tid
        assert str(event.tenant_id) != fake_tid

    def test_spoofed_actor_id_in_body_ignored(self) -> None:
        """Even if a client sends a different actor_id in the request body,
        the audit record uses the trusted ActorContext value.
        """
        real_uid = _uid()
        actor = _make_actor(user_id=real_uid)

        # The builder only accepts ActorContext — no way to inject spoofed id
        event = AuditEventBuilder.from_actor(
            actor=actor,
            action="test",
            action_category=AuditActionCategory.SYSTEM,
        )
        assert str(event.actor_id) == real_uid

    def test_spoofed_tenant_id_in_body_ignored(self) -> None:
        """The builder API does not accept a raw tenant_id — only ActorContext."""
        real_tid = _uid()
        actor = _make_actor(tenant_id=real_tid)

        event = AuditEventBuilder.from_actor(
            actor=actor,
            action="test",
            action_category=AuditActionCategory.SYSTEM,
        )
        # No way to inject a different tenant_id through the builder
        assert str(event.tenant_id) == real_tid


# =============================================================================
# Serialization Round-Trip
# =============================================================================


class TestSerialization:
    """AuditEvent must serialize and deserialize deterministically."""

    def test_round_trip(self) -> None:
        """JSON serialization round-trip preserves semantic equality."""
        actor = _make_actor()
        original = AuditEvent(
            actor_id=actor.actor_id,
            tenant_id=actor.tenant_id,
            action="tenant.created",
            action_category=AuditActionCategory.TENANT,
            correlation_id="req-123",
            venue_id=VenueId(uuid4()),
            metadata={"initiated_by": "system"},
        )
        serialized = original.model_dump_json()
        restored = AuditEvent.model_validate_json(serialized)

        assert restored.actor_id == original.actor_id
        assert restored.tenant_id == original.tenant_id
        assert restored.action == original.action
        assert restored.action_category == original.action_category
        assert restored.correlation_id == original.correlation_id
        assert restored.venue_id == original.venue_id
        assert restored.metadata == original.metadata
        assert restored.schema_version == original.schema_version

    def test_serialization_no_secrets_leak(self) -> None:
        """Serialized JSON must not contain any sensitive data."""
        actor = _make_actor()
        event = AuditEvent(
            actor_id=actor.actor_id,
            tenant_id=actor.tenant_id,
            action="test",
            action_category=AuditActionCategory.SYSTEM,
        )
        json_str = event.model_dump_json()
        assert "password" not in json_str.lower()
        assert "secret" not in json_str.lower()
        assert "token" not in json_str.lower()


# =============================================================================
# AuditActionCategory Enum Tests
# =============================================================================


class TestAuditActionCategory:
    """AuditActionCategory enum tests."""

    def test_all_categories_exist(self) -> None:
        """All expected audit categories are defined."""
        categories = {c.value for c in AuditActionCategory}
        expected = {
            "authentication",
            "authorization",
            "venue",
            "video",
            "analytics",
            "evidence",
            "recommendation",
            "alert",
            "user",
            "membership",
            "tenant",
            "system",
        }
        assert categories == expected

    def test_each_category_is_unique(self) -> None:
        """No duplicate values in the enum."""
        values = [c.value for c in AuditActionCategory]
        assert len(values) == len(set(values))
