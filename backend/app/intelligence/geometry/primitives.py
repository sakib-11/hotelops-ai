"""Shared primitives for the deterministic geometry layer (Task 14 Step 2).

``GEOMETRY_TOLERANCE`` and ``Point2D`` are consumed by both ``polygon``
(the point-in-polygon layer) and ``segments`` (the line-segment layer).
They live in this tiny leaf module so neither module imports the other
— ``polygon`` re-exports them for its historical public surface, and the
package ``__init__`` exposes them once.

The tolerance POLICY rationale is documented at its canonical home in
``polygon.py``; this module only holds the value.
"""

from __future__ import annotations

Point2D = tuple[float, float]

# =============================================================================
# Tolerance policy — the ONLY numeric tolerance in this layer
# =============================================================================
#
# GEOMETRY_TOLERANCE (1e-9) is the single distance/area threshold for
# boundary and degeneracy classification.  It exists because:
#
#   1. Canonical IMAGE_NORMALIZED coordinates are rounded to 1e-6
#      precision (contracts.geometry _PRECISION), so the boundary
#      tolerance must be strictly SMALLER than the smallest meaningful
#      coordinate delta — otherwise canonicalized vertices would be
#      spuriously classified as BOUNDARY.
#   2. 1e-9 is far above machine epsilon (~1e-16), so floating-point
#      noise in point-to-edge distances can never flip a result.
#   3. It is consistent in magnitude with the Task 10.6 validation
#      engine's AREA_TOLERANCE (1e-6) and segment epsilon (1e-12).
#
# Consumers must never scatter ad-hoc values (e.g. 0.000001) through
# their own code; they import GEOMETRY_TOLERANCE or pass it explicitly.
GEOMETRY_TOLERANCE = 1e-9

__all__ = ["GEOMETRY_TOLERANCE", "Point2D"]
