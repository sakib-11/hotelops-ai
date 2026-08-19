"""Deterministic spatial evaluation engines (Task 14 Steps 3, 4, and 5).

Pure zone membership + exclusion evaluation + table mapping
(Steps 3/5, ``engine``) and line-crossing / spatial transitions
(Step 4, ``transitions``): canonical spatial points plus the session's
immutable published configuration version produce canonical
observations. No I/O, no randomness, no fallback to the latest
configuration.

- ``exceptions`` — the typed error taxonomy (``SpatialEvaluationError``
  and subclasses); malformed input is never repaired.
- ``engine`` — Steps 3/5 zone membership, exclusion evaluation, and
  table mapping with combined ambiguity resolution
  (``evaluate_spatial``), reusing the Step 2 geometry layer.
- ``transitions`` — Step 4 line crossing (``evaluate_line_crossing``),
  reusing the Step 2 geometry layer's line-segment primitives.
"""

from backend.app.intelligence.spatial.engine import (
    SpatialEvaluationInput,
    SpatialEvaluationResult,
    TableMembership,
    ZoneMembership,
    evaluate_spatial,
)
from backend.app.intelligence.spatial.exceptions import (
    BoundaryPolicyUndefinedError,
    CameraNotInConfigurationError,
    ConfigurationNotPublishedError,
    InvalidLineError,
    InvalidSpatialInputError,
    LineNotApplicableError,
    ReferenceIntegrityError,
    SpatialEvaluationError,
    TransitionOrderError,
    TransitionScopeError,
    VenuePointRequiredError,
)
from backend.app.intelligence.spatial.transitions import (
    LineCrossingInput,
    evaluate_line_crossing,
)

__all__ = [
    "BoundaryPolicyUndefinedError",
    "CameraNotInConfigurationError",
    "ConfigurationNotPublishedError",
    "InvalidLineError",
    "InvalidSpatialInputError",
    "LineCrossingInput",
    "LineNotApplicableError",
    "ReferenceIntegrityError",
    "SpatialEvaluationError",
    "SpatialEvaluationInput",
    "SpatialEvaluationResult",
    "TableMembership",
    "TransitionOrderError",
    "TransitionScopeError",
    "VenuePointRequiredError",
    "ZoneMembership",
    "evaluate_line_crossing",
    "evaluate_spatial",
]
