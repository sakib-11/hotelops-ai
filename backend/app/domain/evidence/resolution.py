"""Deterministic source asset / video session resolution (Task 17.4).

Resolves the EXACT source recording required by an ``EvidenceRef``:

    EvidenceRef
        ↓ SourceResolver
    ResolvedSourceSegment

Resolution uses ONLY the evidence's canonical scope + identity — tenant,
venue, source (``video_asset_id``), camera, video session, and the
requested time interval. It NEVER:

- resolves by "latest recording" — a historical interval resolves to its
  historical asset, not to the most recent recording;
- searches outside the tenant/venue scope — a candidate from another
  tenant/venue is an ``AUTHORIZATION_FAILURE``, never silently ignored;
- substitutes another camera — a candidate whose camera differs from the
  evidence's camera simply does not match (``SOURCE_NOT_FOUND``).

The resolver is PURE and deterministic (no database, no object storage,
no wall clock): the caller supplies the bounded, tenant/venue-scoped
candidate recordings as ``SourceRecordingCandidate``. Availability
(expired / retained-deleted) is a STATED input — the caller computes
expiry against the wall clock — so the resolver never reads
``datetime.now()``.

Canonical coverage policy (established here — no prior policy existed):

1. Authorization — any candidate outside the evidence's tenant/venue
   scope yields ``AUTHORIZATION_FAILURE``.
2. Identity matching — a candidate matches only when EVERY identity
   present on the evidence (``video_asset_id``, ``camera_id``,
   ``video_session_id``) equals the candidate's, and the candidate is
   available. No identity on the evidence → ``SOURCE_NOT_FOUND``.
3. Coverage walk — matching candidates are clipped to the requested
   interval and sorted deterministically by (clip start, asset_id).
   Walking in order, each recording contributes only the coverage beyond
   the cursor (overlapping recordings: the EARLIEST-start recording owns
   the overlap — the deterministic tie-break); the result is an ordered,
   disjoint list of covered segments.
4. Outcome — the segments cover the full requested interval →
   ``RESOLVED``; some coverage but with gaps → ``PARTIAL_COVERAGE``;
   no coverage → ``SOURCE_NOT_FOUND`` (with a deterministic reason that
   distinguishes "no matching recording" from "recording expired").

Every outcome preserves the evidence provenance (ref id, event id,
tenant, venue, session, camera, requested interval, configuration
version, rule id/version) on the ``ResolvedSourceSegment``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, field_validator, model_validator

from contracts.common import (
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
    validate_utc,
)
from contracts.events import EvidenceRef

__all__ = [
    "ResolvedSourceSegment",
    "SourceRecordingCandidate",
    "SourceResolutionStatus",
    "SourceResolver",
    "SourceSegment",
]


class SourceResolutionStatus(StrEnum):
    """Deterministic outcome of a source resolution (Task 17.4)."""

    RESOLVED = "resolved"
    PARTIAL_COVERAGE = "partial_coverage"
    SOURCE_NOT_FOUND = "source_not_found"
    AUTHORIZATION_FAILURE = "authorization_failure"


class SourceRecordingCandidate(BaseModel, frozen=True):
    """One candidate source recording (caller-supplied, deterministic).

    ``available`` is a STATED input: the caller determines expiry/retention
    against the wall clock; the resolver only honors the flag, so identical
    inputs always resolve identically.
    """

    model_config = {"extra": "forbid"}

    asset_id: VideoAssetId
    tenant_id: TenantId
    venue_id: VenueId
    camera_id: CameraId | None = None
    session_id: VideoSessionId | None = None
    start_time: datetime
    end_time: datetime
    available: bool = True

    _validate_start = field_validator("start_time")(validate_utc)
    _validate_end = field_validator("end_time")(validate_utc)

    @model_validator(mode="after")
    def _validate_window(self) -> SourceRecordingCandidate:
        if self.end_time < self.start_time:
            raise ValueError("end_time must not precede start_time")
        return self


class SourceSegment(BaseModel, frozen=True):
    """One resolved, covered sub-segment of the requested evidence interval."""

    model_config = {"extra": "forbid"}

    asset_id: VideoAssetId
    camera_id: CameraId | None = None
    session_id: VideoSessionId | None = None
    start_time: datetime
    end_time: datetime

    _validate_start = field_validator("start_time")(validate_utc)
    _validate_end = field_validator("end_time")(validate_utc)


class ResolvedSourceSegment(BaseModel, frozen=True):
    """Deterministic resolution of the source recording(s) for an EvidenceRef.

    ``segments`` is non-empty for ``RESOLVED`` and ``PARTIAL_COVERAGE``
    (ordered, disjoint covered sub-segments); ``reason`` carries a
    deterministic explanation for every non-``RESOLVED`` outcome. All
    provenance fields are preserved verbatim from the evidence request —
    never re-derived.
    """

    model_config = {"extra": "forbid"}

    status: SourceResolutionStatus
    # Provenance (preserved from the evidence request)
    evidence_ref_id: EvidenceId
    event_id: EventId | None = None
    tenant_id: TenantId | None = None
    venue_id: VenueId | None = None
    camera_id: CameraId | None = None
    video_session_id: VideoSessionId | None = None
    configuration_version_id: ConfigurationVersionId | None = None
    rule_id: RuleId | None = None
    rule_version: RuleVersion | None = None
    requested_start: datetime
    requested_end: datetime
    segments: tuple[SourceSegment, ...] = ()
    reason: str | None = None

    _validate_requested_start = field_validator("requested_start")(validate_utc)
    _validate_requested_end = field_validator("requested_end")(validate_utc)


class SourceResolver:
    """Deterministic EvidenceRef → ResolvedSourceSegment (Task 17.4)."""

    def resolve(
        self,
        evidence: EvidenceRef,
        recordings: Sequence[SourceRecordingCandidate],
    ) -> ResolvedSourceSegment:
        """Resolve the exact source recording for the evidence interval.

        Args:
            evidence: The canonical evidence request (never modified).
            recordings: The bounded candidate recordings the caller
                already scoped by tenant/venue; the resolver re-validates
                every candidate against the evidence scope.

        Returns:
            A deterministic ResolvedSourceSegment (RESOLVED /
            PARTIAL_COVERAGE / SOURCE_NOT_FOUND / AUTHORIZATION_FAILURE)
            preserving the evidence provenance.
        """
        requested_start = (
            evidence.start_time if evidence.start_time is not None else evidence.event_time
        )
        requested_end = evidence.end_time if evidence.end_time is not None else evidence.event_time

        def outcome(
            status: SourceResolutionStatus,
            *,
            segments: tuple[SourceSegment, ...] = (),
            reason: str | None = None,
        ) -> ResolvedSourceSegment:
            camera_id = evidence.camera_id
            if camera_id is None and segments:
                camera_id = segments[0].camera_id
            return ResolvedSourceSegment(
                status=status,
                evidence_ref_id=evidence.ref_id,
                event_id=evidence.event_id,
                tenant_id=evidence.tenant_id,
                venue_id=evidence.venue_id,
                camera_id=camera_id,
                video_session_id=evidence.video_session_id,
                configuration_version_id=evidence.configuration_version_id,
                rule_id=evidence.rule_id,
                rule_version=evidence.rule_version,
                requested_start=requested_start,
                requested_end=requested_end,
                segments=segments,
                reason=reason,
            )

        # --- 1. Authorization — never resolve outside tenant/venue scope ---
        if evidence.tenant_id is None or evidence.venue_id is None:
            return outcome(
                SourceResolutionStatus.AUTHORIZATION_FAILURE,
                reason="evidence carries no tenant/venue scope — cannot authorize resolution",
            )
        for rec in recordings:
            if rec.tenant_id != evidence.tenant_id:
                return outcome(
                    SourceResolutionStatus.AUTHORIZATION_FAILURE,
                    reason=(
                        f"candidate asset {rec.asset_id} belongs to tenant "
                        f"{rec.tenant_id}, not {evidence.tenant_id} — "
                        f"never resolve another tenant's recordings"
                    ),
                )
            if rec.venue_id != evidence.venue_id:
                return outcome(
                    SourceResolutionStatus.AUTHORIZATION_FAILURE,
                    reason=(
                        f"candidate asset {rec.asset_id} belongs to venue "
                        f"{rec.venue_id}, not {evidence.venue_id} — "
                        f"never resolve another venue's recordings"
                    ),
                )

        # --- 2. Identity matching — exact, never "latest", never substitution ---
        if (
            evidence.video_asset_id is None
            and evidence.camera_id is None
            and evidence.video_session_id is None
        ):
            return outcome(
                SourceResolutionStatus.SOURCE_NOT_FOUND,
                reason="evidence carries no source identity (asset/camera/session)",
            )

        matching_all = [rec for rec in recordings if self._identity_matches(evidence, rec)]
        matching = [rec for rec in matching_all if rec.available]
        if not matching:
            if matching_all:
                return outcome(
                    SourceResolutionStatus.SOURCE_NOT_FOUND,
                    reason="no available recording — the matching recording is expired or retained-deleted",
                )
            return outcome(
                SourceResolutionStatus.SOURCE_NOT_FOUND,
                reason="no recording matches the requested source identity and interval",
            )

        # --- 3. Coverage walk (canonical policy, deterministic) ---
        segments = self._cover(matching, requested_start, requested_end)
        if not segments:
            return outcome(
                SourceResolutionStatus.SOURCE_NOT_FOUND,
                reason="no recording covers the requested interval",
            )

        full = (
            segments[0].start_time <= requested_start
            and segments[-1].end_time >= requested_end
            and all(
                segments[i].start_time <= segments[i - 1].end_time for i in range(1, len(segments))
            )
        )
        if full:
            return outcome(SourceResolutionStatus.RESOLVED, segments=segments)

        # An instant request is either fully covered or not found — there is
        # no meaningful "partial" coverage of a single instant.
        if requested_start == requested_end:
            return outcome(
                SourceResolutionStatus.SOURCE_NOT_FOUND,
                reason="no recording covers the requested instant",
            )
        return outcome(
            SourceResolutionStatus.PARTIAL_COVERAGE,
            segments=segments,
            reason=self._gap_reason(segments, requested_start, requested_end),
        )

    @staticmethod
    def _identity_matches(evidence: EvidenceRef, rec: SourceRecordingCandidate) -> bool:
        """A candidate matches only when EVERY evidence identity equals it."""
        return not (
            (evidence.video_asset_id is not None and rec.asset_id != evidence.video_asset_id)
            or (evidence.camera_id is not None and rec.camera_id != evidence.camera_id)
            or (
                evidence.video_session_id is not None
                and rec.session_id != evidence.video_session_id
            )
        )

    @staticmethod
    def _cover(
        recordings: Sequence[SourceRecordingCandidate],
        start: datetime,
        end: datetime,
    ) -> tuple[SourceSegment, ...]:
        """The ordered, disjoint covered sub-segments of [start, end].

        Deterministic policy: clip each recording to the request, sort by
        (clip start, asset_id), then walk — each recording contributes
        only the coverage beyond the cursor, so the earliest-start
        recording owns any overlap and the result is contiguous-within-
        coverage (gaps are preserved, not concealed).
        """
        clipped = [
            (
                max(rec.start_time, start),
                min(rec.end_time, end),
                rec,
            )
            for rec in recordings
            if min(rec.end_time, end) >= max(rec.start_time, start)
        ]
        clipped.sort(key=lambda item: (item[0], str(item[2].asset_id)))

        segments: list[SourceSegment] = []
        cursor = start
        for lo, hi, rec in clipped:
            if hi <= cursor and segments:
                continue  # fully redundant (touching adds no coverage)
            segment_start = max(lo, cursor)
            segments.append(
                SourceSegment(
                    asset_id=rec.asset_id,
                    camera_id=rec.camera_id,
                    session_id=rec.session_id,
                    start_time=segment_start,
                    end_time=hi,
                )
            )
            cursor = max(cursor, hi)
        return tuple(segments)

    @staticmethod
    def _gap_reason(
        segments: tuple[SourceSegment, ...],
        start: datetime,
        end: datetime,
    ) -> str:
        """Deterministic gap listing for PARTIAL_COVERAGE outcomes."""
        gaps: list[str] = []
        if segments[0].start_time > start:
            gaps.append(f"[{start.isoformat()},{segments[0].start_time.isoformat()})")
        for i in range(1, len(segments)):
            if segments[i].start_time > segments[i - 1].end_time:
                gaps.append(
                    f"[{segments[i - 1].end_time.isoformat()},{segments[i].start_time.isoformat()})"
                )
        if segments[-1].end_time < end:
            gaps.append(f"[{segments[-1].end_time.isoformat()},{end.isoformat()})")
        return "coverage gaps: " + ", ".join(gaps)
