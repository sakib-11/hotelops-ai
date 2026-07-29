"""Canonical audit event model.

Records security/operational audit context for HotelOps AI.
Audit data is derived EXCLUSIVELY from trusted server-side state
(ActorContext), never from client-provided values.

NEVER audit:
    - passwords
    - raw authentication tokens
    - secrets
    - API keys
    - private credentials
    - complete authentication headers
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from contracts.common import (
    SCHEMA_VERSION,
    MembershipId,
    TenantId,
    UserId,
    VenueId,
    validate_schema_version,
)


class AuditActionCategory(StrEnum):
    """High-level categories for auditable actions.

    Kept small and stable. Extend as new operational domains are added.
    """

    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    VENUE = "venue"
    VIDEO = "video"
    ANALYTICS = "analytics"
    EVIDENCE = "evidence"
    RECOMMENDATION = "recommendation"
    ALERT = "alert"
    USER = "user"
    MEMBERSHIP = "membership"
    TENANT = "tenant"
    SYSTEM = "system"


class AuditEvent(BaseModel, frozen=True):
    """An auditable event record.

    Constructed from trusted server-side ActorContext.
    The actor_id, tenant_id, and venue_id are NEVER taken from
    client-supplied request data — they are derived from the
    authenticated and authorized ActorContext.

    Sensitive data (passwords, tokens, secrets, API keys, credentials)
    is NEVER included in audit records.

    Attributes:
        actor_id: The authenticated user who performed the action.
        tenant_id: The tenant scope of the action.
        action: A human-readable description of the action performed.
        action_category: High-level category of the action.
        schema_version: Contract schema version.
        membership_id: Optional membership through which the actor acted.
        venue_id: Optional venue context of the action.
        correlation_id: Optional request/correlation identifier for tracing.
        timestamp: When the audit event was recorded (UTC).
        metadata: Optional non-sensitive metadata about the action.
    """

    model_config = {"extra": "forbid"}

    actor_id: UserId
    tenant_id: TenantId
    action: str = Field(min_length=1, max_length=512)
    action_category: AuditActionCategory
    schema_version: str = Field(default=SCHEMA_VERSION)
    membership_id: MembershipId | None = None
    venue_id: VenueId | None = None
    correlation_id: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, str] | None = None

    _validate_schema = field_validator("schema_version")(validate_schema_version)

    @field_validator("metadata")
    def _reject_secrets_in_metadata(cls, v: dict[str, str] | None) -> dict[str, str] | None:  # ruff: ignore[invalid-first-argument-name-for-method] — Pydantic v2 needs cls
        """Ensure no sensitive keys are stored in audit metadata.

        Never audit: password, token, secret, key, credential.
        Checks the first underscore-delimited segment of each key
        against blocked terms, so "api_key" → first segment "api"
        (allowed) while "secret_key" → first segment "secret" (blocked).
        """
        if v is None:
            return v
        blocked_terms = frozenset({
            "password",
            "token",
            "secret",
            "key",
            "credential",
            "authorization",
        })
        for key in v:
            lower_key = key.lower()
            # Check the first underscore/dot-separated segment
            first_segment = lower_key.replace("-", "_").split("_")[0].split(".")[0]
            if first_segment in blocked_terms:
                raise ValueError(
                    f"Rejected sensitive key in audit metadata: '{key}'. "
                    "Never audit passwords, tokens, secrets, keys, or credentials."
                )
        return v
