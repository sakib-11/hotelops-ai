"""Stable validation rule codes (Task 10.9).

Every rule that the deterministic validation engine can emit has a
stable machine-readable code and a FIXED severity classification. The
severity of a code never varies — identical content + identical
validator version always produces identical findings.

Only ERROR-severity findings block VALIDATED/PUBLISHED. WARNINGS are
reported and recorded but never block publication.
"""

from __future__ import annotations

from enum import StrEnum


class RuleCode(StrEnum):
    """Stable validation rule codes (ERROR unless marked WARNING)."""

    # --- Geometry ---
    GEOMETRY_INVALID = "GEOMETRY_INVALID"
    GEOMETRY_EMPTY = "GEOMETRY_EMPTY"
    GEOMETRY_OUT_OF_RANGE = "GEOMETRY_OUT_OF_RANGE"
    GEOMETRY_SELF_INTERSECTION = "GEOMETRY_SELF_INTERSECTION"
    GEOMETRY_ZERO_AREA = "GEOMETRY_ZERO_AREA"
    GEOMETRY_TYPE_INVALID = "GEOMETRY_TYPE_INVALID"
    GEOMETRY_NOT_CLOSED = "GEOMETRY_NOT_CLOSED"

    # --- Coordinate space ---
    COORDINATE_SPACE_INVALID = "COORDINATE_SPACE_INVALID"
    COORDINATE_MIXED_SPACES = "COORDINATE_MIXED_SPACES"

    # --- Structure / references ---
    DUPLICATE_IDENTIFIER = "DUPLICATE_IDENTIFIER"
    MISSING_REFERENCE = "MISSING_REFERENCE"
    CROSS_VERSION_REFERENCE = "CROSS_VERSION_REFERENCE"
    CROSS_TENANT_REFERENCE = "CROSS_TENANT_REFERENCE"

    # --- Spatial policy ---
    INVALID_CONTAINMENT = "INVALID_CONTAINMENT"
    TABLE_OVERLAP = "TABLE_OVERLAP"
    INVALID_SPATIAL_RELATIONSHIP = "INVALID_SPATIAL_RELATIONSHIP"
    ENTITY_GEOMETRY_CONTRACT_VIOLATION = "ENTITY_GEOMETRY_CONTRACT_VIOLATION"

    # --- Camera ---
    CAMERA_REFERENCE_INVALID = "CAMERA_REFERENCE_INVALID"
    CAMERA_RETIRED = "CAMERA_RETIRED"
    CAMERA_UNAVAILABLE = "CAMERA_UNAVAILABLE"
    CAMERA_PROFILE_INCOMPATIBLE = "CAMERA_PROFILE_INCOMPATIBLE"

    # --- Privacy / exclusion policy ---
    PRIVACY_POLICY_CONFLICT = "PRIVACY_POLICY_CONFLICT"
    EXCLUSION_POLICY_CONFLICT = "EXCLUSION_POLICY_CONFLICT"

    # --- CV compatibility (WARNINGS) ---
    ZONE_UNCOVERED = "ZONE_UNCOVERED"  # WARNING
    CAMERA_NO_CONFIGURED_COVERAGE = "CAMERA_NO_CONFIGURED_COVERAGE"  # WARNING


# Codes that are WARNINGS (informational, never block publication).
_WARNING_CODES: frozenset[RuleCode] = frozenset({
    RuleCode.ZONE_UNCOVERED,
    RuleCode.CAMERA_NO_CONFIGURED_COVERAGE,
})


def severity_of(code: RuleCode) -> str:
    """Deterministic severity classification — never varies per input."""
    return "warning" if code in _WARNING_CODES else "error"


__all__ = [
    "RuleCode",
    "severity_of",
]
