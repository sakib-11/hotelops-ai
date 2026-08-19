"""Complete auditable evidence package (Task 17.7).

Connects the FULL provenance chain of a material operational event to
its stored evidence:

    Material Event
        → EvidenceRef          (the request/ref: event linkage, source,
                                 session, camera, time/frame, processing
                                 versions, configuration version, rule
                                 version, requested checksum, storage ref)
        → ResolvedSourceSegment (Task 17.4: exact source asset + session
                                 + camera + requested interval resolution)
        → ExtractedEvidence     (Task 17.5/17.6: ACTUAL extracted window,
                                 frame range, media facts, checksum,
                                 storage reference, extraction status)
        → Stored Evidence

The package COMPOSES the three canonical models — it never duplicates
their fields, and it never re-derives provenance. ``EvidencePackage`` is
the aggregate root that:

- preserves EVERY provenance field of the chain (event_id, evidence_id,
  tenant/venue scope, source/session/camera identity, requested + actual
  intervals, frame interval, checksum, storage reference, configuration
  version, detector/tracker versions, rule id/version, extraction
  status, provenance metadata);
- refuses to finalize when provenance would be LOST or CONTRADICTED
  (``EvidencePackageBuilder.finalize`` validates cross-model identity
  consistency and completeness — e.g. a SUCCESS extraction without a
  storage reference is a provenance loss and is rejected);
- is deterministic (Task 7 idempotency): the package identity is
  content-derived from the evidence request + extraction, so replaying
  the same inputs produces the same package;
- exposes ``provenance_chain()`` — the ordered, typed audit trail of
  every hop from event to stored evidence.

The package carries no wall clock of its own: ``created_at`` is left to
the persistence/fulfillment layer so replay stays deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid5

from pydantic import BaseModel, Field, field_validator

from backend.app.domain.evidence.extraction import (
    ExtractedEvidence,
    ExtractionStatus,
)
from backend.app.domain.evidence.resolution import ResolvedSourceSegment
from contracts.common import (
    SCHEMA_VERSION,
    CameraId,
    ConfigurationVersionId,
    EventId,
    EvidenceId,
    RuleId,
    RuleVersion,
    TenantId,
    VenueId,
    VideoAssetId,
    VideoSessionId,
    validate_schema_version,
    validate_utc,
)
from contracts.events import EvidenceRef
from contracts.temporal import TEMPORAL_ID_NAMESPACE

__all__ = [
    "EvidencePackage",
    "EvidencePackageBuilder",
    "ProvenanceHop",
]


@dataclass(frozen=True)
class ProvenanceHop:
    """One typed hop in the evidence provenance chain (audit trail).

    ``position`` is the deterministic 1-based index of the hop in the
    chain; ``link`` names the hop (e.g. ``event -> evidence``) and
    ``value`` is the canonical value carried across that hop. Hops are
    derived from the composed models — never re-derived from raw input.
    """

    position: int
    link: str
    value: str | None

    def __str__(self) -> str:
        return f"{self.position}. {self.link}: {self.value}"


class EvidencePackage(BaseModel, frozen=True):
    """The complete auditable evidence package for one material event.

    Composes the canonical ``EvidenceRef``, ``ResolvedSourceSegment``
    and ``ExtractedEvidence`` — provenance is preserved by composition,
    and ``finalize()`` guarantees no hop in the chain is lost or
    contradicted.
    """

    model_config = {"extra": "forbid"}

    package_id: EvidenceId
    schema_version: str = Field(default=SCHEMA_VERSION)
    # The composed canonical chain.
    evidence_ref: EvidenceRef
    resolved_source: ResolvedSourceSegment
    extraction: ExtractedEvidence
    # Fulfillment-layer timestamp (None in a deterministic replay).
    created_at: datetime | None = None

    _validate_schema = field_validator("schema_version")(validate_schema_version)
    _validate_created = field_validator("created_at")(validate_utc)

    # ------------------------------------------------------------------
    # Preserved provenance (read-only views over the composed models)
    # ------------------------------------------------------------------

    @property
    def event_id(self) -> EventId | None:
        return self.evidence_ref.event_id

    @property
    def evidence_id(self) -> EvidenceId:
        return self.evidence_ref.ref_id

    @property
    def tenant_id(self) -> TenantId | None:
        return self.evidence_ref.tenant_id

    @property
    def venue_id(self) -> VenueId | None:
        return self.evidence_ref.venue_id

    @property
    def source_asset_id(self) -> VideoAssetId | None:
        return self.evidence_ref.video_asset_id

    @property
    def video_session_id(self) -> VideoSessionId | None:
        return self.evidence_ref.video_session_id

    @property
    def camera_id(self) -> CameraId | None:
        return self.evidence_ref.camera_id

    @property
    def configuration_version_id(self) -> ConfigurationVersionId | None:
        return self.evidence_ref.configuration_version_id

    @property
    def detector_version(self) -> str | None:
        return self.evidence_ref.detector_version

    @property
    def tracker_version(self) -> str | None:
        return self.evidence_ref.tracker_version

    @property
    def rule_id(self) -> RuleId | None:
        return self.evidence_ref.rule_id

    @property
    def rule_version(self) -> RuleVersion | None:
        return self.evidence_ref.rule_version

    @property
    def checksum(self) -> str | None:
        return self.extraction.metadata.get("checksum_sha256") or self.evidence_ref.checksum

    @property
    def storage_reference(self) -> str | None:
        return self.extraction.media_path or self.evidence_ref.ref_uri

    @property
    def extraction_status(self) -> ExtractionStatus:
        return self.extraction.status

    @property
    def requested_start(self) -> datetime:
        return self.extraction.requested_start

    @property
    def requested_end(self) -> datetime:
        return self.extraction.requested_end

    @property
    def actual_start_time(self) -> datetime | None:
        return self.extraction.actual_start_time

    @property
    def actual_end_time(self) -> datetime | None:
        return self.extraction.actual_end_time

    @property
    def start_frame(self) -> int | None:
        return self.extraction.start_frame

    @property
    def end_frame(self) -> int | None:
        return self.extraction.end_frame

    @property
    def media_format(self) -> str | None:
        return self.extraction.media_format

    @property
    def duration_seconds(self) -> float | None:
        return self.extraction.duration_seconds

    @property
    def size_bytes(self) -> int | None:
        return self.extraction.size_bytes

    # ------------------------------------------------------------------
    # Audit trail
    # ------------------------------------------------------------------

    def provenance_chain(self) -> tuple[ProvenanceHop, ...]:
        """The ordered audit trail: Material Event → ... → Stored Evidence.

        Every hop is derived from the composed models (never re-derived
        from raw input), and every hop the evidence carries is present —
        the chain is the deterministic, complete provenance of the
        package.
        """
        ref = self.evidence_ref
        extraction = self.extraction

        hops: list[ProvenanceHop] = []
        position = 0

        def hop(link: str, value: str | None) -> None:
            nonlocal position
            position += 1
            hops.append(ProvenanceHop(position=position, link=link, value=value))

        # Material Event → EvidenceRef
        hop("event -> evidence", _id_or(ref.event_id))
        # EvidenceRef → Source Asset
        hop("evidence -> source", _id_or(ref.video_asset_id))
        # Source Asset → Video Session
        hop("source -> session", _id_or(ref.video_session_id))
        # Video Session → Camera
        hop("session -> camera", _id_or(ref.camera_id))
        # Camera → Frame/Time (requested interval)
        requested = _interval(extraction.requested_start, extraction.requested_end)
        hop("camera -> requested_time", requested)
        hop("camera -> frame_range", _frame_range(ref.start_frame, ref.end_frame))
        # Frame/Time → Processing Versions
        hop("time -> detector_version", ref.detector_version)
        hop("time -> tracker_version", ref.tracker_version)
        # Processing Versions → Configuration Version
        hop("processing -> configuration", _id_or(ref.configuration_version_id))
        # Configuration Version → Rule Version
        hop("configuration -> rule", _rule(ref.rule_id, ref.rule_version))
        # Rule Version → Checksum
        hop("rule -> checksum", self.checksum)
        # Checksum → Stored Evidence
        hop("checksum -> stored_evidence", self.storage_reference)

        return tuple(hops)

    def chain_value(self, link: str) -> str | None:
        """The value of a hop by link name (convenience for audit tests)."""
        for hop in self.provenance_chain():
            if hop.link == link:
                return hop.value
        return None


class EvidencePackageBuilder:
    """Builds a complete auditable EvidencePackage from canonical inputs.

    ``finalize`` is the ONLY way a package is created. It validates that
    the three composed models agree on every shared provenance field and
    that a completed extraction did not lose its storage reference —
    i.e. finalized evidence NEVER loses provenance.
    """

    def finalize(
        self,
        *,
        evidence_ref: EvidenceRef,
        resolved_source: ResolvedSourceSegment,
        extraction: ExtractedEvidence,
        created_at: datetime | None = None,
    ) -> EvidencePackage:
        # --- Cross-model identity consistency (never silently diverge) ---
        self._require_equal(
            "evidence ref id",
            evidence_ref.ref_id,
            resolved_source.evidence_ref_id,
            extraction.evidence_ref_id,
        )
        self._require_equal(
            "event id",
            evidence_ref.event_id,
            resolved_source.event_id,
            extraction.event_id,
        )
        self._require_equal_optional(
            "tenant id",
            evidence_ref.tenant_id,
            resolved_source.tenant_id,
            extraction.tenant_id,
        )
        self._require_equal_optional(
            "venue id",
            evidence_ref.venue_id,
            resolved_source.venue_id,
            extraction.venue_id,
        )
        self._require_equal_optional(
            "session id",
            evidence_ref.video_session_id,
            resolved_source.video_session_id,
            extraction.session_id,
        )
        self._require_equal_optional(
            "camera id",
            evidence_ref.camera_id,
            resolved_source.camera_id,
            extraction.camera_id,
        )
        self._require_equal_optional(
            "configuration version id",
            evidence_ref.configuration_version_id,
            resolved_source.configuration_version_id,
            extraction.configuration_version_id,
        )
        self._require_equal_optional(
            "rule id",
            evidence_ref.rule_id,
            resolved_source.rule_id,
            extraction.rule_id,
        )
        self._require_equal_optional(
            "rule version",
            evidence_ref.rule_version,
            resolved_source.rule_version,
            extraction.rule_version,
        )

        # --- Requested interval consistency ---
        self._require_equal(
            "requested start",
            evidence_ref.start_time
            if evidence_ref.start_time is not None
            else evidence_ref.event_time,
            resolved_source.requested_start,
            extraction.requested_start,
        )
        self._require_equal(
            "requested end",
            evidence_ref.end_time if evidence_ref.end_time is not None else evidence_ref.event_time,
            resolved_source.requested_end,
            extraction.requested_end,
        )

        # --- No provenance loss on completion ---
        # A SUCCESS/PARTIAL extraction must have ACTUALLY produced a
        # storage reference and an integrity checksum. The request's
        # ref_uri/checksum are the request provenance, not the stored
        # artifact — requiring the extraction's own media_path/checksum
        # is what prevents finalized evidence from losing its stored
        # location and integrity proof.
        if extraction.status in (ExtractionStatus.SUCCESS, ExtractionStatus.PARTIAL):
            if not extraction.media_path:
                msg = (
                    "finalize refused: a completed extraction must carry "
                    "its actual storage reference (media_path) — evidence "
                    "would lose its stored location"
                )
                raise ValueError(msg)
            if not self._checksum_of(extraction):
                msg = (
                    "finalize refused: a completed extraction must carry "
                    "its actual integrity checksum — evidence would lose "
                    "its integrity provenance"
                )
                raise ValueError(msg)

        # --- Deterministic package identity (Task 7 idempotency) ---
        package_id = self._package_id(evidence_ref, extraction)

        return EvidencePackage(
            package_id=package_id,
            evidence_ref=evidence_ref,
            resolved_source=resolved_source,
            extraction=extraction,
            created_at=created_at,
        )

    @staticmethod
    def _package_id(evidence_ref: EvidenceRef, extraction: ExtractedEvidence) -> EvidenceId:
        canonical = (
            f"evidence_package|{evidence_ref.ref_id}|{extraction.extraction_id}|"
            f"{extraction.status.value}"
        )
        return EvidenceId(uuid5(TEMPORAL_ID_NAMESPACE, canonical))

    @staticmethod
    def _checksum_of(extraction: ExtractedEvidence) -> str | None:
        checksum = extraction.metadata.get("checksum_sha256")
        return checksum if isinstance(checksum, str) else None

    @staticmethod
    def _require_equal(label: str, *values: object) -> None:
        expected = values[0]
        for value in values[1:]:
            if value != expected:
                msg = (
                    f"finalize refused: inconsistent {label} across the "
                    f"evidence chain ({expected!r} != {value!r}) — "
                    f"provenance would be contradicted"
                )
                raise ValueError(msg)

    @staticmethod
    def _require_equal_optional(label: str, *values: object) -> None:
        present = [v for v in values if v is not None]
        if not present:
            return
        expected = present[0]
        for value in present[1:]:
            if value != expected:
                msg = (
                    f"finalize refused: inconsistent {label} across the "
                    f"evidence chain ({expected!r} != {value!r}) — "
                    f"provenance would be contradicted"
                )
                raise ValueError(msg)


def _id_or(value: object) -> str | None:
    return str(value) if value is not None else None


def _interval(start: datetime, end: datetime) -> str:
    return f"[{start.isoformat()},{end.isoformat()}]"


def _frame_range(start: int | None, end: int | None) -> str | None:
    if start is None and end is None:
        return None
    return f"[{start},{end}]"


def _rule(rule_id: Any, rule_version: Any) -> str | None:
    if rule_id is None:
        return None
    return f"{rule_id}:{rule_version}" if rule_version is not None else str(rule_id)
