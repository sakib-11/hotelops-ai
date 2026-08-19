"""Configuration Domain Service (Task 10.3, 10.10, 10.11).

Orchestrates the ConfigurationVersion lifecycle end-to-end:

  - Draft creation (from scratch or cloned from an existing version)
  - Draft updates (immutable model copies — DRAFT state only)
  - Validation (DRAFT -> VALIDATING -> VALIDATED; failure returns to
    DRAFT and records the validation result)
  - Atomic publication (VALIDATED -> PUBLISHED): locks the venue
    configuration row, verifies the validation result belongs to the
    EXACT current content revision, verifies zero blocking errors, and
    updates the current-published pointer — all in one transaction.
  - Session pinning resolution (exact pinned version, never the latest)

Every state transition is tenant/venue authorized and state-guarded;
every important transition emits an audit/outbox event in the caller's
transaction (config_audit).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.application.services.config_audit import (
    EVENT_DRAFT_CREATED,
    EVENT_DRAFT_UPDATED,
    EVENT_PUBLISHED,
    EVENT_VALIDATION_COMPLETED,
    EVENT_VALIDATION_STARTED,
    enqueue_config_audit_event,
)
from backend.app.domain.configuration.state_machine import (
    ConfigurationImmutableError,
    ConfigurationStateMachine,
    ConfigurationTransitionError,
)
from backend.app.domain.configuration.validation.engine import (
    VALIDATOR_VERSION,
    ConfigurationValidationEngine,
    ValidationOutcome,
)
from backend.app.infrastructure.database.models.configuration import (
    ConfigurationModel,
    ConfigurationVersionModel,
)
from backend.app.infrastructure.database.repositories.configuration import (
    ConfigurationRepository,
    ConfigurationVersionRepository,
)
from contracts.common import (
    ConfigurationId,
    ConfigurationVersionId,
    TenantId,
    VenueId,
)
from contracts.configuration import (
    CameraProfileModel,
    ConfigurationStatus,
    EntranceModel,
    ExclusionROIModel,
    PrivacyROIModel,
    QueueAreaModel,
    ServiceAreaModel,
    TableModel,
    ValidationResultModel,
    ZoneModel,
)
from contracts.configuration import (
    ConfigurationVersionModel as ContractVersion,
)
from contracts.identity import ActorContext


def _conflict_from_state_error(exc: ConfigurationTransitionError) -> ConfigurationConflictError:
    """Translate a state-machine violation into an API-consistent conflict."""
    return ConfigurationConflictError(str(exc))


def _immutable_from_state_error(exc: ConfigurationImmutableError) -> ConfigurationError:
    """Translate a PUBLISHED-immutability violation."""
    return ConfigurationImmutablePublishedError(str(exc))


class ConfigurationError(Exception):
    """Base class for configuration service errors."""


class ConfigurationNotFoundError(ConfigurationError):
    """Requested configuration/version does not exist in the actor's scope."""


class ConfigurationConflictError(ConfigurationError):
    """State conflict (e.g. editing a non-DRAFT, stale validation)."""


class ConfigurationStaleValidationError(ConfigurationError):
    """A validation result no longer matches the current content revision."""


class ConfigurationImmutablePublishedError(ConfigurationConflictError):
    """Attempted mutation of a PUBLISHED (immutable) version."""


@dataclass(frozen=True)
class PublishResult:
    success: bool
    configuration_version_id: uuid.UUID
    previous_published_version_id: uuid.UUID | None
    published_at: datetime


def _now() -> datetime:
    return datetime.now(UTC)


def _clone_with(model: ContractVersion, **updates: Any) -> ContractVersion:
    """Immutable update: rebuild the frozen model with changed fields."""
    return model.model_copy(update=updates)


class ConfigurationService:
    """Repository-backed configuration lifecycle orchestrator."""

    def __init__(
        self,
        *,
        configuration_repository: ConfigurationRepository | None = None,
        version_repository: ConfigurationVersionRepository | None = None,
        validation_engine: ConfigurationValidationEngine | None = None,
    ) -> None:
        self._config_repo = configuration_repository or ConfigurationRepository()
        self._version_repo = version_repository or ConfigurationVersionRepository()
        self._validator = validation_engine or ConfigurationValidationEngine()

    @property
    def validator_version(self) -> str:
        return VALIDATOR_VERSION

    # =========================================================================
    # Draft creation
    # =========================================================================

    async def create_draft(
        self,
        session: AsyncSession,
        actor: ActorContext,
        *,
        venue_id: VenueId | uuid.UUID,
        name: str,
        created_by: str,
        correlation_id: str | None = None,
    ) -> tuple[ContractVersion, ConfigurationModel]:
        """Create a new empty DRAFT version (or first version)."""
        config = await self._config_repo.get_or_create(
            session, actor, venue_id=VenueId(venue_id), name=name
        )
        latest = await self._version_repo.get_latest_version(
            session, actor, config.configuration_id
        )
        next_version = (latest.version + 1) if latest else 1

        row = await self._version_repo.create(
            session,
            actor,
            configuration_id=config.configuration_id,
            venue_id=config.venue_id,
            version_number=next_version,
        )
        draft = ContractVersion(
            configuration_version_id=ConfigurationVersionId(row.configuration_version_id),
            configuration_id=ConfigurationId(config.configuration_id),
            venue_id=VenueId(config.venue_id),
            tenant_id=TenantId(config.tenant_id),
            version=next_version,
            status=ConfigurationStatus.DRAFT,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        await enqueue_config_audit_event(
            session,
            actor=actor,
            event_type=EVENT_DRAFT_CREATED,
            version=row,
            correlation_id=correlation_id,
        )
        return draft, config

    async def create_draft_from_version(
        self,
        session: AsyncSession,
        actor: ActorContext,
        *,
        source_version_id: ConfigurationVersionId | uuid.UUID,
        created_by: str,
        correlation_id: str | None = None,
    ) -> ContractVersion:
        """Clone an existing version (published or draft) into a new DRAFT."""
        source = await self._version_repo.get_for_actor(session, actor, source_version_id)
        if source is None:
            msg = f"Configuration version {source_version_id} not found"
            raise ConfigurationNotFoundError(msg)

        source_contract = await self._hydrate_version(session, source)
        config = await self._config_repo.get_for_actor(session, actor, source.configuration_id)
        if config is None:
            msg = f"Configuration {source.configuration_id} not found"
            raise ConfigurationNotFoundError(msg)
        latest = await self._version_repo.get_latest_version(
            session, actor, config.configuration_id
        )
        next_version = (latest.version + 1) if latest else 1

        row = await self._version_repo.create(
            session,
            actor,
            configuration_id=config.configuration_id,
            venue_id=config.venue_id,
            version_number=next_version,
        )
        draft = _clone_with(
            source_contract,
            configuration_version_id=row.configuration_version_id,
            version=next_version,
            status=ConfigurationStatus.DRAFT,
            validated_at=None,
            validated_by=None,
            validation_result=None,
            validation_errors=[],
            published_at=None,
            published_by=None,
            replaced_version_id=None,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        await self._version_repo.replace_entities(session, row, draft)
        await enqueue_config_audit_event(
            session,
            actor=actor,
            event_type=EVENT_DRAFT_CREATED,
            version=row,
            correlation_id=correlation_id,
            extra_payload={"cloned_from": str(source_version_id)},
        )
        return draft

    # =========================================================================
    # Draft updates
    # =========================================================================

    async def update_draft(
        self,
        session: AsyncSession,
        actor: ActorContext,
        *,
        version_id: ConfigurationVersionId | uuid.UUID,
        cameras: list[CameraProfileModel] | None = None,
        zones: list[ZoneModel] | None = None,
        tables: list[TableModel] | None = None,
        entrances: list[EntranceModel] | None = None,
        queue_areas: list[QueueAreaModel] | None = None,
        service_areas: list[ServiceAreaModel] | None = None,
        privacy_rois: list[PrivacyROIModel] | None = None,
        exclusion_rois: list[ExclusionROIModel] | None = None,
        correlation_id: str | None = None,
    ) -> ContractVersion:
        """Replace the entities of a DRAFT version (immutable copy)."""
        row = await self._version_repo.get_for_actor(session, actor, version_id)
        if row is None:
            msg = f"Configuration version {version_id} not found"
            raise ConfigurationNotFoundError(msg)

        try:
            ConfigurationStateMachine.assert_can_edit(
                ConfigurationStatus(row.status), str(version_id)
            )
        except ConfigurationTransitionError as exc:
            raise _conflict_from_state_error(exc) from exc
        except ConfigurationImmutableError as exc:
            raise _immutable_from_state_error(exc) from exc
        current = await self._hydrate_version(session, row)
        updates: dict[str, Any] = {}
        if cameras is not None:
            updates["cameras"] = cameras
        if zones is not None:
            updates["zones"] = zones
        if tables is not None:
            updates["tables"] = tables
        if entrances is not None:
            updates["entrances"] = entrances
        if queue_areas is not None:
            updates["queue_areas"] = queue_areas
        if service_areas is not None:
            updates["service_areas"] = service_areas
        if privacy_rois is not None:
            updates["privacy_rois"] = privacy_rois
        if exclusion_rois is not None:
            updates["exclusion_rois"] = exclusion_rois
        updated = _clone_with(current, updated_at=_now(), **updates)

        # Invalidate any prior validation (content changed): the contract
        # snapshot AND the ORM row must both drop the stale validation
        # metadata so the DB layer never shows outdated validation state.
        if updated.validation_result is not None:
            updated = _clone_with(
                updated,
                validation_result=None,
                validation_errors=[],
                validated_at=None,
                validated_by=None,
            )
            row.validation_result = None
            row.validated_at = None
            row.validated_by = None
        await self._version_repo.replace_entities(session, row, updated)
        await enqueue_config_audit_event(
            session,
            actor=actor,
            event_type=EVENT_DRAFT_UPDATED,
            version=row,
            correlation_id=correlation_id,
        )
        return updated

    # =========================================================================
    # Validation lifecycle
    # =========================================================================

    async def start_validation(
        self,
        session: AsyncSession,
        actor: ActorContext,
        *,
        version_id: ConfigurationVersionId | uuid.UUID,
        correlation_id: str | None = None,
    ) -> ContractVersion:
        """Transition DRAFT -> VALIDATING (state machine enforced)."""
        row = await self._version_repo.get_for_actor(session, actor, version_id)
        if row is None:
            msg = f"Configuration version {version_id} not found"
            raise ConfigurationNotFoundError(msg)
        try:
            ConfigurationStateMachine.assert_can_validate(
                ConfigurationStatus(row.status), str(version_id)
            )
        except ConfigurationTransitionError as exc:
            raise _conflict_from_state_error(exc) from exc
        ok = await self._version_repo.update_status(
            session,
            actor,
            version_id=version_id,
            from_status=ConfigurationStatus.DRAFT.value,
            to_status=ConfigurationStatus.VALIDATING.value,
        )
        if not ok:
            msg = f"Version {version_id} is not in DRAFT state"
            raise ConfigurationConflictError(msg)
        row.status = ConfigurationStatus.VALIDATING.value
        await enqueue_config_audit_event(
            session,
            actor=actor,
            event_type=EVENT_VALIDATION_STARTED,
            version=row,
            correlation_id=correlation_id,
        )
        return _clone_with(
            await self._hydrate_version(session, row), status=ConfigurationStatus.VALIDATING
        )

    async def run_validation(
        self,
        session: AsyncSession,
        actor: ActorContext,
        *,
        version_id: ConfigurationVersionId | uuid.UUID,
        validated_by: str,
        correlation_id: str | None = None,
    ) -> tuple[ContractVersion, ValidationResultModel]:
        """Run the deterministic engine and record the result.

        On success: VALIDATING -> VALIDATED (result stored).
        On failure: VALIDATING -> DRAFT (result stored, editable again).
        """
        row = await self._version_repo.get_for_actor(session, actor, version_id)
        if row is None:
            msg = f"Configuration version {version_id} not found"
            raise ConfigurationNotFoundError(msg)
        if row.status != ConfigurationStatus.VALIDATING.value:
            msg = f"Version {version_id} is not in VALIDATING state"
            raise ConfigurationConflictError(msg)

        version = await self._hydrate_version(session, row)
        content_revision = version.content_revision()
        outcome: ValidationOutcome = await self._validator.validate(version)
        result = outcome.to_result_model(
            version=version,
            content_revision=content_revision,
            validated_by=validated_by,
            validated_at=_now(),
        )

        if outcome.valid:
            ok = await self._version_repo.update_status(
                session,
                actor,
                version_id=version_id,
                from_status=ConfigurationStatus.VALIDATING.value,
                to_status=ConfigurationStatus.VALIDATED.value,
                extra_updates={
                    "validation_result": result.model_dump(mode="json"),
                    "validated_at": result.validated_at,
                    "validated_by": validated_by,
                    "validation_errors": [],
                },
            )
            if not ok:
                msg = f"Version {version_id} is not in VALIDATING state"
                raise ConfigurationConflictError(msg)
            row.status = ConfigurationStatus.VALIDATED.value
            row.validation_result = result.model_dump(mode="json")
            row.validated_at = result.validated_at
            row.validated_by = validated_by
            final = _clone_with(
                version, status=ConfigurationStatus.VALIDATED, validation_result=result
            )
        else:
            # Failed validation returns the version to DRAFT (editable).
            ok = await self._version_repo.update_status(
                session,
                actor,
                version_id=version_id,
                from_status=ConfigurationStatus.VALIDATING.value,
                to_status=ConfigurationStatus.DRAFT.value,
                extra_updates={
                    "validation_result": result.model_dump(mode="json"),
                    "validation_errors": [e.message for e in outcome.errors],
                    "validated_at": None,
                    "validated_by": None,
                },
            )
            if not ok:
                msg = f"Version {version_id} is not in VALIDATING state"
                raise ConfigurationConflictError(msg)
            row.status = ConfigurationStatus.DRAFT.value
            row.validation_result = result.model_dump(mode="json")
            final = _clone_with(version, status=ConfigurationStatus.DRAFT, validation_result=result)

        await enqueue_config_audit_event(
            session,
            actor=actor,
            event_type=EVENT_VALIDATION_COMPLETED,
            version=row,
            correlation_id=correlation_id,
            extra_payload={
                "valid": result.valid,
                "error_count": len(result.errors),
                "warning_count": len(result.warnings),
            },
        )
        return final, result

    # =========================================================================
    # Publication
    # =========================================================================

    async def publish(
        self,
        session: AsyncSession,
        actor: ActorContext,
        *,
        version_id: ConfigurationVersionId | uuid.UUID,
        published_by: str,
        correlation_id: str | None = None,
    ) -> PublishResult:
        """Atomically publish a VALIDATED version (idempotent, stale-safe).

        Guarantees:
          - version is VALIDATED (state machine)
          - validation result belongs to the EXACT current content
            revision (stale validation rejected)
          - zero blocking validation errors
          - row lock on the venue configuration serializes concurrent
            publish/validation
          - version -> PUBLISHED and current-published pointer update
            commit ATOMICALLY with the audit/outbox rows
        """
        row = await self._version_repo.get_for_actor(session, actor, version_id)
        if row is None:
            msg = f"Configuration version {version_id} not found"
            raise ConfigurationNotFoundError(msg)

        # Idempotent replay: an already-published version returns its
        # stored result (no duplicate business record).
        if row.status == ConfigurationStatus.PUBLISHED.value:
            return PublishResult(
                success=True,
                configuration_version_id=row.configuration_version_id,
                previous_published_version_id=row.replaced_version_id,
                published_at=row.published_at or _now(),
            )

        try:
            ConfigurationStateMachine.assert_can_publish(
                ConfigurationStatus(row.status), str(version_id)
            )
        except ConfigurationTransitionError as exc:
            raise _conflict_from_state_error(exc) from exc
        if row.status != ConfigurationStatus.VALIDATED.value:
            msg = f"Version {version_id} is not in VALIDATED state"
            raise ConfigurationConflictError(msg)

        # Verify the stored validation result belongs to this exact revision.
        version = await self._hydrate_version(session, row)
        current_revision = version.content_revision()
        result_model: ValidationResultModel | None = (
            ValidationResultModel.model_validate(row.validation_result)
            if row.validation_result
            else None
        )
        if result_model is None:
            msg = f"Version {version_id} has no validation result"
            raise ConfigurationStaleValidationError(msg)
        if result_model.content_revision != current_revision:
            msg = (
                f"Validation result for version {version_id} is stale: content "
                "changed since validation — re-validate before publishing"
            )
            raise ConfigurationStaleValidationError(msg)
        if result_model.blocking_errors:
            msg = (
                f"Version {version_id} has {len(result_model.blocking_errors)} "
                "blocking validation errors — cannot publish"
            )
            raise ConfigurationConflictError(msg)

        config = await self._version_repo.lock_venue_configuration(
            session, actor, row.configuration_id
        )
        if config is None:
            msg = f"Configuration {row.configuration_id} not found"
            raise ConfigurationNotFoundError(msg)
        previous_published = config.current_published_version_id

        # Monotonicity guard (P1): the venue's current-version pointer
        # must never regress. After the row lock, verify no HIGHER
        # version is already published for this configuration — if one
        # is, publishing this (older) version would silently move the
        # active configuration backwards, so it is rejected.
        newer_published = await self._version_repo.get_latest_published_version(
            session, actor, row.configuration_id
        )
        if newer_published is not None and newer_published.version > row.version:
            msg = (
                f"Version {row.version} cannot be published: version "
                f"{newer_published.version} is already the published "
                "configuration for this venue — publish the newest version"
            )
            raise ConfigurationConflictError(msg)

        now = _now()
        ok = await self._version_repo.update_status(
            session,
            actor,
            version_id=version_id,
            from_status=ConfigurationStatus.VALIDATED.value,
            to_status=ConfigurationStatus.PUBLISHED.value,
            extra_updates={
                "published_at": now,
                "published_by": published_by,
                "replaced_version_id": previous_published,
            },
        )
        if not ok:
            msg = f"Version {version_id} is not in VALIDATED state"
            raise ConfigurationConflictError(msg)
        row.status = ConfigurationStatus.PUBLISHED.value
        row.published_at = now
        row.published_by = published_by
        row.replaced_version_id = previous_published

        pointer_ok = await self._config_repo.set_current_published_version(
            session,
            actor,
            configuration_id=row.configuration_id,
            configuration_version_id=row.configuration_version_id,
        )
        if not pointer_ok:
            msg = f"Configuration {row.configuration_id} pointer update failed"
            raise ConfigurationConflictError(msg)

        await enqueue_config_audit_event(
            session,
            actor=actor,
            event_type=EVENT_PUBLISHED,
            version=row,
            correlation_id=correlation_id,
            extra_payload={
                "previous_published_version_id": str(previous_published)
                if previous_published
                else None
            },
        )
        return PublishResult(
            success=True,
            configuration_version_id=row.configuration_version_id,
            previous_published_version_id=previous_published,
            published_at=now,
        )

    # =========================================================================
    # Reads / session pinning
    # =========================================================================

    async def get_version(
        self,
        session: AsyncSession,
        actor: ActorContext,
        version_id: ConfigurationVersionId | uuid.UUID,
    ) -> ContractVersion | None:
        row = await self._version_repo.get_for_actor(session, actor, version_id)
        if row is None:
            return None
        return await self._hydrate_version(session, row)

    async def list_versions(
        self,
        session: AsyncSession,
        actor: ActorContext,
        configuration_id: ConfigurationId | uuid.UUID,
    ) -> list[ContractVersion]:
        rows = await self._version_repo.get_by_configuration(session, actor, configuration_id)
        return [await self._hydrate_version(session, r) for r in rows]

    async def resolve_current_published(
        self,
        session: AsyncSession,
        actor: ActorContext,
        venue_id: VenueId | uuid.UUID,
    ) -> ContractVersion | None:
        """Resolve the CURRENT published version for a venue."""
        row = await self._version_repo.get_current_published_for_venue(session, actor, venue_id)
        if row is None:
            return None
        return await self._hydrate_version(session, row)

    async def resolve_session_configuration(
        self,
        session: AsyncSession,
        actor: ActorContext,
        session_id: uuid.UUID | str,
    ) -> ContractVersion | None:
        """Resolve the EXACT pinned version of a session (never the latest).

        Returns None when the session is unpinned — callers must reject
        unpinned sessions (a session must reference exactly one immutable
        published configuration version).
        """
        row = await self._version_repo.get_published_for_session(session, actor, session_id)
        if row is None:
            return None
        return await self._hydrate_version(session, row)

    # =========================================================================
    # Hydration: ORM row -> contract snapshot (with stored entities)
    # =========================================================================

    async def _hydrate_version(
        self, session: AsyncSession, row: ConfigurationVersionModel
    ) -> ContractVersion:
        """Build the full contract snapshot (entities included)."""
        return await self._version_repo.load_contract(session, row)


__all__ = [
    "ConfigurationConflictError",
    "ConfigurationError",
    "ConfigurationImmutablePublishedError",
    "ConfigurationNotFoundError",
    "ConfigurationService",
    "ConfigurationStaleValidationError",
    "PublishResult",
]
