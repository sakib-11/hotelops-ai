"""Exception taxonomy for the deterministic spatial evaluation engine (Task 14 Step 3).

Mirrors the project's provider-isolation convention (detectors,
``tracking``, ``geometry``): downstream business logic depends only on
these types, never on raw ``ValueError``/math errors leaking from
predicate internals.

Semantics:

- ``SpatialEvaluationError`` is the base for every spatial evaluation
  failure — the direct analog of ``TrackingError`` (Task 13 boundary)
  and ``GeometryError`` (Task 14 Step 2 boundary).
- ``InvalidSpatialInputError`` — the evaluation inputs are missing or
  are not the canonical contract types (configuration/track/camera/
  point), or the spatial point is non-finite / out of bounds. Malformed
  inputs are never repaired or clamped.
- ``ConfigurationNotPublishedError`` — the supplied configuration
  version is not PUBLISHED (draft/validating/validated). A session must
  be pinned to exactly one immutable PUBLISHED version; the engine
  never falls back to "the latest" configuration.
- ``CameraNotInConfigurationError`` — the physical ``camera_id`` is not
  present in the pinned configuration version (never evaluated against
  another camera's geometry).
- ``ReferenceIntegrityError`` — the camera declares a zone/ROI profile
  id that is missing from the pinned version, or the referenced
  geometry violates the entity geometry contract (zone must be POLYGON
  in VENUE_LOCAL; ROI must be POLYGON in IMAGE_NORMALIZED or
  VENUE_LOCAL) or the polygon-ring contract. Re-asserted defensively at
  this boundary because a corrupted snapshot is never silently
  repaired.
- ``BoundaryPolicyUndefinedError`` — the Step 2 geometry layer
  classified the point BOUNDARY while Task 10 defines no boundary
  membership policy. BOUNDARY is NEVER silently converted to INSIDE or
  OUTSIDE; this is the recorded blocker (see ``engine`` module
  docstring).
- ``VenuePointRequiredError`` — zone membership / line crossing and
  venue-scoped exclusion/privacy evaluation require a VENUE_LOCAL
  point, but the input point is IMAGE_NORMALIZED while the camera
  declares venue geometry. The camera→venue projection (ADR-010
  INV-CS-01, ``CameraCalibration``) does not exist in the Task 10
  configuration model — this is the recorded blocker; the engine never
  fabricates a projection and never silently reports OUTSIDE/NO_CROSSING.
- ``TransitionScopeError`` (Step 4) — the previous/current observations
  do not share track or session scope; a transition is only defined
  between consecutive observations of the SAME track.
- ``TransitionOrderError`` (Step 4) — the current observation does not
  advance the frame sequence (duplicate frame id) or regresses the
  event time; a transition is never manufactured from invalid input.
- ``LineNotApplicableError`` (Step 4) — the configured line is not
  bound to the evaluated camera (camera isolation).
- ``InvalidLineError`` (Step 4) — the configured line is not a valid
  LINESTRING or uses a foreign coordinate space.

All failures are deterministic: identical input always produces the
same typed error.
"""

from __future__ import annotations


class SpatialEvaluationError(Exception):
    """Base exception for all spatial evaluation engine errors."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: {self.message}>"


class InvalidSpatialInputError(SpatialEvaluationError):
    """Evaluation inputs are missing, wrong-typed, or geometrically invalid."""


class ConfigurationNotPublishedError(SpatialEvaluationError):
    """The pinned configuration version is not PUBLISHED (never latest)."""


class CameraNotInConfigurationError(SpatialEvaluationError):
    """The physical camera is not part of the pinned configuration version."""


class ReferenceIntegrityError(SpatialEvaluationError):
    """A camera-declared zone/ROI is missing or malformed in the version."""


class BoundaryPolicyUndefinedError(SpatialEvaluationError):
    """A BOUNDARY classification with no Task 10 boundary policy (blocker)."""


class VenuePointRequiredError(SpatialEvaluationError):
    """Zone membership / line crossing needs a VENUE_LOCAL point (blocker)."""


class TransitionScopeError(SpatialEvaluationError):
    """The previous/current observations are not the same track/session."""


class TransitionOrderError(SpatialEvaluationError):
    """The current observation does not advance the frame sequence."""


class LineNotApplicableError(SpatialEvaluationError):
    """The configured line is not bound to the evaluated camera."""


class InvalidLineError(SpatialEvaluationError):
    """The configured line is not a valid LINESTRING or space-matches none."""


__all__ = [
    "BoundaryPolicyUndefinedError",
    "CameraNotInConfigurationError",
    "ConfigurationNotPublishedError",
    "InvalidLineError",
    "InvalidSpatialInputError",
    "LineNotApplicableError",
    "ReferenceIntegrityError",
    "SpatialEvaluationError",
    "TransitionOrderError",
    "TransitionScopeError",
    "VenuePointRequiredError",
]
