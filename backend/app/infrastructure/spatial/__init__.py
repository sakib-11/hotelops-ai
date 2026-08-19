"""PostGIS-backed spatial engine (Task 10.6).

Production-authoritative implementation of the domain SpatialEngine
protocol: spatial calculations run through PostGIS ST_* functions on
the canonical JSONB geometry (ST_GeomFromGeoJSON), NOT hand-written
polygon math. The deterministic validation engine accepts any
SpatialEngine — pure-Python (offline/tests) or this PostGIS engine
(production).
"""

from backend.app.infrastructure.spatial.engine import PostGISGeometryEngine

__all__ = ["PostGISGeometryEngine"]
