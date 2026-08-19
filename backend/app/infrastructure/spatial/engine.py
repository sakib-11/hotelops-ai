"""PostGIS geometry engine — authoritative spatial calculations (Task 10.6).

Implements the domain ``SpatialEngine`` protocol using PostgreSQL
PostGIS functions (ST_GeomFromGeoJSON, ST_Area, ST_Contains,
ST_Intersection, ST_Touches, ST_IsValid, ST_IsSimple). PostGIS is the
authoritative spatial engine: the validation engine's spatial checks
delegate here in production, while offline/tests use the deterministic
pure-Python engine behind the same protocol.

Geometry is stored as canonical JSONB (contracts.geometry.GeometryModel
serialization); the migration creates GIST expression indexes on
ST_GeomFromGeoJSON(geometry) so spatial predicates are indexed.
"""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from backend.app.domain.configuration.validation.spatial import (
    AREA_TOLERANCE,
    SpatialEngine,
)
from contracts.geometry import GeometryModel, GeometryType


class PostGISGeometryEngine(SpatialEngine):
    """PostGIS-backed spatial engine (production-authoritative)."""

    def __init__(self, session: AsyncSession | AsyncConnection) -> None:
        self._session = session

    # =========================================================================
    # Geometry serialization
    # =========================================================================

    @staticmethod
    def _geojson(geometry: GeometryModel) -> str:
        """Canonical GeoJSON payload for ST_GeomFromGeoJSON."""
        gtype = {
            GeometryType.POINT: "Point",
            GeometryType.LINESTRING: "LineString",
            GeometryType.POLYGON: "Polygon",
        }[geometry.geometry_type]
        coords: list[list[float]] = geometry.canonicalize().coordinates
        payload: list[list[float]] | list[list[list[float]]] = coords
        if gtype == "Polygon":
            payload = [coords]  # GeoJSON polygon is a ring list
        return json.dumps({"type": gtype, "coordinates": payload})

    # =========================================================================
    # SpatialEngine protocol
    # =========================================================================

    async def overlap_area(self, a: GeometryModel, b: GeometryModel) -> float:
        """Area of the intersection (PostGIS authoritative)."""
        sql = text(
            "SELECT COALESCE(ST_Area(ST_Intersection("
            "ST_GeomFromGeoJSON(:a), ST_GeomFromGeoJSON(:b))), 0.0)"
        )
        result = await self._session.execute(sql, {"a": self._geojson(a), "b": self._geojson(b)})
        return float(result.scalar() or 0.0)

    async def contains(self, outer: GeometryModel, inner: GeometryModel) -> bool:
        """True when ``outer`` spatially contains ``inner``."""
        sql = text("SELECT ST_Contains(ST_GeomFromGeoJSON(:outer), ST_GeomFromGeoJSON(:inner))")
        result = await self._session.execute(
            sql, {"outer": self._geojson(outer), "inner": self._geojson(inner)}
        )
        return bool(result.scalar())

    async def boundary_touches(self, a: GeometryModel, b: GeometryModel) -> bool:
        """Boundary contact without meaningful area overlap."""
        sql = text("SELECT ST_Touches(ST_GeomFromGeoJSON(:a), ST_GeomFromGeoJSON(:b))")
        result = await self._session.execute(sql, {"a": self._geojson(a), "b": self._geojson(b)})
        return bool(result.scalar())

    async def is_valid_polygon(self, geometry: GeometryModel) -> bool:
        """PostGIS validity: ST_IsValid AND ST_IsSimple AND area > tolerance."""
        sql = text(
            "SELECT ST_IsValid(g) AND ST_IsSimple(g) AND ST_Area(g) > :tol "
            "FROM (SELECT ST_GeomFromGeoJSON(:geom) AS g) AS s"
        )
        result = await self._session.execute(
            sql, {"geom": self._geojson(geometry), "tol": AREA_TOLERANCE}
        )
        return bool(result.scalar())

    async def meaningful_overlap(self, a: GeometryModel, b: GeometryModel) -> bool:
        """True when overlap area exceeds the documented tolerance."""
        return await self.overlap_area(a, b) > AREA_TOLERANCE


__all__ = ["PostGISGeometryEngine"]
