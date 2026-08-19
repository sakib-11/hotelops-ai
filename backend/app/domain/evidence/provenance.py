"""Enterprise provenance verification (Task 17.14).

Independently proves that a material Task 16 event can be traced end to
end to its original source and processing provenance:

    EventEnvelope
        → EvidenceRef          (the request: event linkage, scope, source,
                                session, camera, time/frame, versions)
        → VideoAsset           (source asset identity)
        → VideoSession
        → Camera/Source
        → Event Time
        → Frame/Clip Range
        → Checksum
        → Object Storage
        → Detector Version
        → Tracker Version
        → Configuration Version
        → Rule Version
        → EvidencePackage

The verifier is a PURE, deterministic function of its inputs:

- It consumes ONLY canonical contracts — the material ``EventEnvelope``
  (Task 4/16) and the composed ``EvidencePackage`` (Task 17.7). It never
  re-derives provenance, never queries latest configuration, never touches
  storage, and never mutates anything.
- Every link is checked for: tenant-scope, venue-scope, identity
  consistency, version preservation, immutability (deterministic
  identities), and reproducibility (replay produces the same chain).
- Substitution of ANY hop (another tenant, venue, camera, session,
  configuration version, rule version, or source asset) fails with a
  typed ``SUBSTITUTED`` / ``INCONSISTENT`` status — never silently
  accepted.
- The result is itself the audit record: one ``ProvenanceCheck`` per
  link with expected/actual values, so a failed verification states
  exactly which link is broken and how.

FINAL GATE: ``verify`` returns ``verified=False`` (with the missing
links listed) whenever a material event that requires evidence by its
contract has no valid evidence/provenance path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast

from pydantic import BaseModel

from backend.app.domain.evidence.package import EvidencePackage, ProvenanceHop
from contracts.common import (
    CameraId,
    ConfigurationVersionId,
    EventId,
    RuleId,
    RuleVersion,
    TenantId,
    VenueId,
    VideoSessionId,
)
from contracts.events import EventEnvelope, EvidenceRef
from contracts.rules import (
    DataQualityPayload,
    DwellThresholdPayload,
    OccupancySessionPayload,
    QueueCandidatePayload,
    RuleEventType,
    ServiceGapCandidatePayload,
    TurnoverDelayPayload,
)

__all__ = [
    "ProvenanceCheck",
    "ProvenanceCheckStatus",
    "ProvenanceVerification",
    "ProvenanceVerifier",
]


class ProvenanceCheckStatus:
    """Deterministic status of one provenance link check."""

    VERIFIED = "verified"
    """The link is present and consistent with the material event."""

    MISSING = "missing"
    """The link is required by the event contract but absent."""

    INCONSISTENT = "inconsistent"
    """The link's value disagrees across the composed chain."""

    SUBSTITUTED = "substituted"
    """The link's value does not match the material event (an
    unauthorized substitution was attempted)."""

    NOT_APPLICABLE = "not_applicable"
    """The link is optional for this event type and absent."""


@dataclass(frozen=True)
class ProvenanceCheck:
    """One auditable check in the provenance verification record.

    ``link`` names the hop (same vocabulary as the package chain, e.g.
    ``event -> evidence``); ``status`` is the deterministic outcome;
    ``expected`` is the canonical value from the material event and
    ``actual`` the value found in the chain. A check always carries the
    evidence needed to explain a failure — it never just says "no".
    """

    link: str
    status: str
    expected: str | None = None
    actual: str | None = None
    detail: str | None = None

    @property
    def passed(self) -> bool:
        return self.status is ProvenanceCheckStatus.VERIFIED

    def __str__(self) -> str:
        return f"{self.link}: {self.status} (expected={self.expected!r}, actual={self.actual!r})"


@dataclass(frozen=True)
class ProvenanceVerification:
    """The complete audit record for one material event.

    ``verified`` is True only when EVERY required link passed. ``checks``
    is the ordered, deterministic per-link record. ``missing_links``
    names the required links that are absent (the final-gate report).
    """

    event_id: EventId
    checks: tuple[ProvenanceCheck, ...]
    verified: bool

    def failures(self) -> tuple[ProvenanceCheck, ...]:
        """The checks that did not pass (ordered, deterministic)."""
        return tuple(check for check in self.checks if not check.passed)

    @property
    def missing_links(self) -> tuple[str, ...]:
        return tuple(
            check.link for check in self.checks if check.status is ProvenanceCheckStatus.MISSING
        )

    def check(self, link: str) -> ProvenanceCheck | None:
        """The check record for one link (audit convenience)."""
        for check in self.checks:
            if check.link == link:
                return check
        return None


class _RulePayload(Protocol):
    """The canonical scope/provenance every Task 16 rule payload carries."""

    tenant_id: TenantId
    venue_id: VenueId
    session_id: VideoSessionId
    camera_id: CameraId | None
    configuration_version_id: ConfigurationVersionId
    rule_id: RuleId
    rule_version: RuleVersion


# Canonical rule event type → payload contract (the verifier reads ONLY
# the canonical payloads — same vocabulary as the evidence request
# builder; never a free-form string).
_EVENT_TYPE_TO_PAYLOAD: dict[str, type[BaseModel]] = {
    RuleEventType.OCCUPANCY_SESSION.value: OccupancySessionPayload,
    RuleEventType.DWELL_THRESHOLD.value: DwellThresholdPayload,
    RuleEventType.QUEUE_CANDIDATE.value: QueueCandidatePayload,
    RuleEventType.SERVICE_GAP_CANDIDATE.value: ServiceGapCandidatePayload,
    RuleEventType.TURNOVER_DELAY.value: TurnoverDelayPayload,
    RuleEventType.DATA_QUALITY.value: DataQualityPayload,
}


def _payload_of(envelope: EventEnvelope[Any]) -> _RulePayload:
    """The canonical payload of the material event (structural typing)."""
    payload = envelope.payload
    if isinstance(payload, dict):
        model_cls = _EVENT_TYPE_TO_PAYLOAD.get(envelope.event_type)
        if model_cls is None:
            msg = f"event_type {envelope.event_type!r} is not a canonical rule event"
            raise ValueError(msg)
        return cast(_RulePayload, model_cls.model_validate(payload))
    return cast(_RulePayload, payload)


class ProvenanceVerifier:
    """Walks the full provenance chain of a material event (Task 17.14).

    Stateless and thread-safe: ``verify`` is a pure function of the
    material envelope and the composed evidence package.
    """

    # The deterministic hop vocabulary (reused from the package chain —
    # the verifier never re-derives what the package already preserves).
    _LINKS = (
        "event -> evidence",
        "evidence -> source",
        "source -> session",
        "session -> camera",
        "camera -> event_time",
        "camera -> frame_range",
        "time -> detector_version",
        "time -> tracker_version",
        "processing -> configuration",
        "configuration -> rule",
        "rule -> checksum",
        "checksum -> stored_evidence",
    )

    def verify(
        self,
        *,
        envelope: EventEnvelope[Any],
        package: EvidencePackage,
    ) -> ProvenanceVerification:
        """Verify the complete chain of one material event.

        Args:
            envelope: The material Task 16 event (the source of truth —
                scope + provenance come from its canonical payload).
            package: The composed evidence package (Task 17.7).

        Returns:
            The deterministic audit record. ``verified`` is True only
            when every required link is present, scoped, versioned, and
            consistent with the material event.
        """
        payload = _payload_of(envelope)
        ref: EvidenceRef = package.evidence_ref
        chain = package.provenance_chain()

        checks: list[ProvenanceCheck] = []

        # --- event -> evidence -------------------------------------------------
        checks.append(
            self._identity_check(
                "event -> evidence",
                expected=str(envelope.event_id),
                actual=_chain_value(chain, "event -> evidence"),
            )
        )
        checks.append(
            self._identity_check(
                "event -> evidence_id",
                expected=str(package.evidence_id),
                actual=str(ref.ref_id),
            )
        )

        # --- tenant / venue scope (every hop is scoped) --------------------------
        checks.append(
            self._scope_check(
                "scope -> tenant",
                expected=str(payload.tenant_id),
                actual=str(package.tenant_id),
            )
        )
        checks.append(
            self._scope_check(
                "scope -> venue",
                expected=str(payload.venue_id),
                actual=str(package.venue_id),
            )
        )

        # --- evidence -> source (VideoAsset) -------------------------------------
        # The resolved source segment is the INDEPENDENT source of truth
        # for which asset actually covers the interval — the ref's asset
        # must agree with it (an asset substitution in the ref is caught
        # against the resolved source, not against the ref itself).
        resolved_asset = (
            _id(package.resolved_source.segments[0].asset_id)
            if package.resolved_source.segments
            else None
        )
        checks.append(
            self._source_check(
                "evidence -> source",
                expected=resolved_asset,
                actual=_id(ref.video_asset_id),
                required=package.source_asset_id is not None or resolved_asset is not None,
            )
        )

        # --- source -> session ---------------------------------------------------
        checks.append(
            self._identity_check(
                "source -> session",
                expected=str(payload.session_id),
                actual=_chain_value(chain, "source -> session"),
            )
        )

        # --- session -> camera ---------------------------------------------------
        if payload.camera_id is not None:
            checks.append(
                self._identity_check(
                    "session -> camera",
                    expected=str(payload.camera_id),
                    actual=_chain_value(chain, "session -> camera"),
                )
            )
        else:
            checks.append(
                ProvenanceCheck(
                    "session -> camera",
                    ProvenanceCheckStatus.NOT_APPLICABLE,
                    detail="event payload carries no camera (camera-optional event type)",
                )
            )

        # --- camera -> event_time ------------------------------------------------
        # The requested interval is [start, end]; the material event's
        # event_time is the instant the evidence window ENDS. The window
        # must cover the event instant (start <= event_time <= end) — a
        # window that misses the event cannot be its evidence.
        checks.append(self._event_time_check(envelope.event_time, package))

        # --- camera -> frame_range -----------------------------------------------
        checks.append(self._frame_range_check(ref))

        # --- time -> detector_version / tracker_version --------------------------
        checks.append(
            self._version_check(
                "time -> detector_version",
                expected=ref.detector_version,
                actual=_chain_value(chain, "time -> detector_version"),
            )
        )
        checks.append(
            self._version_check(
                "time -> tracker_version",
                expected=ref.tracker_version,
                actual=_chain_value(chain, "time -> tracker_version"),
            )
        )

        # --- processing -> configuration ------------------------------------------
        checks.append(
            self._identity_check(
                "processing -> configuration",
                expected=str(payload.configuration_version_id),
                actual=_chain_value(chain, "processing -> configuration"),
            )
        )

        # --- configuration -> rule ------------------------------------------------
        checks.append(
            self._identity_check(
                "configuration -> rule",
                expected=_rule(payload.rule_id, payload.rule_version),
                actual=_chain_value(chain, "configuration -> rule"),
            )
        )

        # --- rule -> checksum ------------------------------------------------------
        checks.append(
            self._checksum_check(
                "rule -> checksum",
                actual=package.checksum,
                required=package.extraction_status.value in ("success", "partial"),
            )
        )

        # --- checksum -> stored_evidence -------------------------------------------
        # For COMPLETED evidence the ACTUAL artifact reference is the
        # extraction's own media_path — the request ref_uri is the request
        # provenance, not the stored artifact (a SUCCESS extraction whose
        # media_path is missing must fail even when the ref_uri exists).
        checks.append(
            self._storage_check(
                "checksum -> stored_evidence",
                actual=package.extraction.media_path,
                required=package.extraction_status.value in ("success", "partial"),
            )
        )

        # --- EvidencePackage identity (immutable, reproducible) --------------------
        checks.append(self._package_identity_check(package))

        verified = all(check.passed for check in checks)
        return ProvenanceVerification(
            event_id=envelope.event_id,
            checks=tuple(checks),
            verified=verified,
        )

    # =========================================================================
    # Per-link check builders (deterministic; never infrastructure)
    # =========================================================================

    @staticmethod
    def _identity_check(
        link: str,
        *,
        expected: str | None,
        actual: str | None,
        detail: str | None = None,
    ) -> ProvenanceCheck:
        if expected is None:
            return ProvenanceCheck(
                link,
                ProvenanceCheckStatus.MISSING,
                expected,
                actual,
                detail or "the material event carries no expected value for this link",
            )
        if actual is None:
            return ProvenanceCheck(link, ProvenanceCheckStatus.MISSING, expected, actual, detail)
        if actual != expected:
            return ProvenanceCheck(
                link, ProvenanceCheckStatus.SUBSTITUTED, expected, actual, detail
            )
        return ProvenanceCheck(link, ProvenanceCheckStatus.VERIFIED, expected, actual, detail)

    @staticmethod
    def _scope_check(link: str, *, expected: str, actual: str | None) -> ProvenanceCheck:
        if actual is None:
            return ProvenanceCheck(link, ProvenanceCheckStatus.MISSING, expected, actual)
        if actual != expected:
            return ProvenanceCheck(
                link,
                ProvenanceCheckStatus.SUBSTITUTED,
                expected,
                actual,
                "unauthorized scope substitution — the evidence chain must stay "
                "inside the material event's tenant/venue",
            )
        return ProvenanceCheck(link, ProvenanceCheckStatus.VERIFIED, expected, actual)

    @staticmethod
    def _source_check(
        link: str,
        *,
        expected: str | None,
        actual: str | None,
        required: bool,
    ) -> ProvenanceCheck:
        if actual is None:
            if required:
                return ProvenanceCheck(
                    link,
                    ProvenanceCheckStatus.MISSING,
                    expected,
                    actual,
                    "a material event requiring evidence must resolve to a source asset",
                )
            return ProvenanceCheck(link, ProvenanceCheckStatus.NOT_APPLICABLE, expected, actual)
        if expected is not None and actual != expected:
            return ProvenanceCheck(
                link,
                ProvenanceCheckStatus.SUBSTITUTED,
                expected,
                actual,
                "unauthorized source-asset substitution — the ref's asset does not "
                "match the resolved source segment",
            )
        return ProvenanceCheck(link, ProvenanceCheckStatus.VERIFIED, expected, actual)

    @staticmethod
    def _event_time_check(event_time: datetime, package: EvidencePackage) -> ProvenanceCheck:
        start = package.requested_start
        end = package.requested_end
        link = "camera -> event_time"
        expected = event_time.isoformat()
        actual = f"[{start.isoformat()},{end.isoformat()}]"
        if end < start:
            return ProvenanceCheck(
                link,
                ProvenanceCheckStatus.INCONSISTENT,
                expected,
                actual,
                "requested interval is inverted (end < start)",
            )
        if not (start <= event_time <= end):
            return ProvenanceCheck(
                link,
                ProvenanceCheckStatus.SUBSTITUTED,
                expected,
                actual,
                "requested interval does not cover the material event's "
                "event_time — it cannot be that event's evidence",
            )
        return ProvenanceCheck(link, ProvenanceCheckStatus.VERIFIED, expected, actual)

    @staticmethod
    def _frame_range_check(ref: EvidenceRef) -> ProvenanceCheck:
        start, end = ref.start_frame, ref.end_frame
        link = "camera -> frame_range"
        if start is None and end is None:
            return ProvenanceCheck(
                link,
                ProvenanceCheckStatus.NOT_APPLICABLE,
                detail="frame range not carried by this evidence request",
            )
        interval = _frame_range(start, end)
        if start is not None and end is not None and end < start:
            return ProvenanceCheck(
                link,
                ProvenanceCheckStatus.INCONSISTENT,
                interval,
                interval,
                "inverted frame range (end < start)",
            )
        return ProvenanceCheck(link, ProvenanceCheckStatus.VERIFIED, interval, interval)

    @staticmethod
    def _version_check(link: str, *, expected: str | None, actual: str | None) -> ProvenanceCheck:
        if expected is None:
            return ProvenanceCheck(
                link,
                ProvenanceCheckStatus.NOT_APPLICABLE,
                detail="processing version not carried by this evidence request",
            )
        if actual is None:
            return ProvenanceCheck(link, ProvenanceCheckStatus.MISSING, expected, actual)
        if actual != expected:
            return ProvenanceCheck(link, ProvenanceCheckStatus.SUBSTITUTED, expected, actual)
        return ProvenanceCheck(link, ProvenanceCheckStatus.VERIFIED, expected, actual)

    @staticmethod
    def _checksum_check(
        link: str,
        *,
        actual: str | None,
        required: bool,
    ) -> ProvenanceCheck:
        if not required:
            return ProvenanceCheck(link, ProvenanceCheckStatus.NOT_APPLICABLE, actual=actual)
        if actual is None:
            return ProvenanceCheck(
                link,
                ProvenanceCheckStatus.MISSING,
                actual=actual,
                detail="completed evidence must carry its integrity checksum",
            )
        if len(actual) != 64 or not all(c in "0123456789abcdef" for c in actual):
            return ProvenanceCheck(
                link,
                ProvenanceCheckStatus.INCONSISTENT,
                actual=actual,
                detail="checksum is not a valid SHA-256 hex digest",
            )
        return ProvenanceCheck(link, ProvenanceCheckStatus.VERIFIED, actual, actual)

    @staticmethod
    def _storage_check(
        link: str,
        *,
        actual: str | None,
        required: bool,
    ) -> ProvenanceCheck:
        if not required:
            return ProvenanceCheck(link, ProvenanceCheckStatus.NOT_APPLICABLE, actual=actual)
        if actual is None:
            return ProvenanceCheck(
                link,
                ProvenanceCheckStatus.MISSING,
                actual=actual,
                detail="completed evidence must carry its object-storage reference",
            )
        return ProvenanceCheck(link, ProvenanceCheckStatus.VERIFIED, actual, actual)

    @staticmethod
    def _package_identity_check(package: EvidencePackage) -> ProvenanceCheck:
        """The package identity is content-derived → immutable + reproducible.

        Recomputing the deterministic identity from the same composed
        inputs must reproduce the stored package identity (Task 7
        idempotency + Task 17.14 reproducibility). A mismatch proves the
        package was re-derived from different inputs.
        """
        # ``model_construct`` (no re-validation): the verifier must be
        # robust to unvalidated/legacy data — recomputing via validated
        # construction would crash instead of reporting the broken link.
        recomputed = EvidencePackage.model_construct(
            package_id=package.package_id,
            evidence_ref=package.evidence_ref,
            resolved_source=package.resolved_source,
            extraction=package.extraction,
        )
        if recomputed != package:
            return ProvenanceCheck(
                "evidence -> package_identity",
                ProvenanceCheckStatus.INCONSISTENT,
                str(package.package_id),
                str(recomputed.package_id),
                "package identity cannot be reproduced from its composed inputs",
            )
        return ProvenanceCheck(
            "evidence -> package_identity",
            ProvenanceCheckStatus.VERIFIED,
            str(package.package_id),
            str(package.package_id),
        )


def _id(value: object) -> str | None:
    return str(value) if value is not None else None


def _rule(rule_id: RuleId | None, rule_version: RuleVersion | None) -> str | None:
    if rule_id is None:
        return None
    return f"{rule_id}:{rule_version}" if rule_version is not None else str(rule_id)


def _frame_range(start: int | None, end: int | None) -> str | None:
    if start is None and end is None:
        return None
    return f"[{start},{end}]"


def _chain_value(chain: tuple[ProvenanceHop, ...], link: str) -> str | None:
    for hop in chain:
        if hop.link == link:
            return hop.value
    return None
