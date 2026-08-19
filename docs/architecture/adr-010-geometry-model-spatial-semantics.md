# ADR-010: Geometry Model & Spatial Semantics

**Status**: Approved
**Date**: 2026-08-10
**Deciders**: Senior Software Architect, Lead CV Engineer, Platform Lead
**Tags**: `cv-engine`, `spatial`, `geometry`, `postgis`, `domain-model`, `contract`

---

## 1. Executive Summary & Objective

This Architecture Decision Record (ADR) establishes the **Geometry Model & Spatial Semantics** as the immutable contract governing all spatial reasoning within the HotelOps Computer Vision (CV) Engine.

The primary objective is to **freeze the geometry contract** prior to any database migrations or application code implementation. This document serves as the single Source of Truth (SoT) for:
1.  **Coordinate Space Definitions**: Strict separation of Camera-relative vs. Venue-relative spaces.
2.  **Primitive Semantics**: Authoritative mapping of GeoJSON primitives to CV domain concepts.
3.  **Validation Pipeline**: Multi-layered integrity guarantees from schema to business policy.
4.  **Error Taxonomy**: Machine-readable, structured error responses for automation.

**Scope**: Applies to all services producing, consuming, or persisting spatial geometry (CV Inference Pipeline, Zone Management API, Analytics Engine, Floor Plan Editor).

---

## 2. Core Principles

| Principle | Description |
| :--- | :--- |
| **Geometry is Versioned CV State** | Geometry artifacts are snapshots bound to a `ConfigurationVersion`. They are **not** frontend drawing data, UI annotations, or transient inference outputs. |
| **Immutability** | Once persisted against a version, geometry records **MUST NOT** be mutated. Corrections require a new `ConfigurationVersion`. |
| **Coordinate Space Fidelity** | A geometry object **MUST** declare its `coordinateSpace`. Cross-space operations (e.g., `ST_Intersects` between `IMAGE_NORMALIZED` and `VENUE_LOCAL`) are **FORBIDDEN** at the database and application layer. |
| **Privacy Precedence** | Spatial validation hierarchy: `PRIVACY_MASK` > `EXCLUSION_ZONE` > Standard CV Zones. Privacy geometry is architecturally supreme. |
| **Policy-Driven Validity** | Geometric overlap (`A ∩ B ≠ ∅`) is **not** an intrinsic error. Validity is determined exclusively by the **Overlap Policy Matrix** (defined in Task 10.5). |

---

## 3. Spatial Domain Model: Coordinate Spaces

The system operates in two distinct, non-interoperable coordinate spaces. **All geometry MUST declare exactly one.**

### 3.1 `IMAGE_NORMALIZED` (Camera-Relative)
*   **Domain**: Unit square `[0.0, 1.0] × [0.0, 1.0]`.
*   **Origin**: Top-Left `(0.0, 0.0)`.
*   **Axes**: `X` → Right (Width), `Y` → Down (Height).
*   **Reference Frame**: The decoded video frame buffer dimensions (post-letterbox/pillarbox removal).
*   **Precision**: `DOUBLE PRECISION` (IEEE 754). Minimum significant digits: 6 (approx. 0.1px at 1080p).
*   **Serialization**: GeoJSON `coordinates` array (lon/lat order mapped to x/y).

### 3.2 `VENUE_LOCAL` (Venue-Relative / Metric)
*   **Domain**: Real-world metric coordinates (meters).
*   **Origin**: Arbitrary venue datum (e.g., Building Entrance, SW Corner of Level 0).
*   **Axes**: `X` → Easting, `Y` → Northing (Standard Cartesian / Local Tangent Plane).
*   **Reference Frame**: Defined by `VenueCoordinateReferenceSystem` (CRS) entity (EPSG code or custom affine transform to WGS84).
*   **Z-Coordinate**: Optional `Z` (elevation in meters) for multi-floor venues.
*   **Precision**: `DOUBLE PRECISION`. Tolerance `ε = 0.001` (1mm).

### 3.3 Coordinate Space Invariants
*   **INV-CS-01**: A `Camera` entity **MUST** possess a `CameraCalibration` record (Homography Matrix `H` or Intrinsics/Extrinsics) to project `IMAGE_NORMALIZED` ↔ `VENUE_LOCAL`.
*   **INV-CS-02**: Geometry **MUST NOT** be stored in a "mixed" space. Homogenization is the responsibility of the **Domain Service Layer**, not the Database.
*   **INV-CS-03**: `IMAGE_NORMALIZED` geometries **MUST** be validated against the specific `Camera.resolution` (Width × Height) active at the `ConfigurationVersion`.

---

## 4. Geometric Primitive Definitions (GeoJSON Subset)

The system restricts GeoJSON Geometry Objects to three types. `GeometryCollection`, `MultiPoint`, `MultiLineString`, `MultiPolygon` are **FORBIDDEN** at the persistence layer. Complex topologies are modeled via **Entity Relations** (1:N Geometry per Entity).

| Primitive Type | GeoJSON `type` | Coordinate Array Dimension | Semantic Constraint |
| :--- | :--- | :--- | :--- |
| **Point** | `Point` | `[x, y]` or `[x, y, z]` | Zero-dimensional location. |
| **LineString** | `LineString` | `[[x, y], ...]` (N ≥ 2) | Ordered vertices. **MUST** be simple (no self-intersections). |
| **Polygon** | `Polygon` | `[[[x, y], ...]]` (N ≥ 4, Closed) | **Exterior Ring Only**. **MUST** be valid (Simple, Non-self-intersecting, CCW winding for exterior). **Interior Rings (Holes) ARE FORBIDDEN** at this layer; holes are modeled as separate `EXCLUSION_ZONE` entities with `OverlapPolicy: EXCLUDE`. |

### 4.1 Serialization Canonical Form
```json
{
  "type": "Polygon",
  "coordinates": [[[x1, y1], [x2, y2], [x3, y3], [x1, y1]]],
  "coordinateSpace": "VENUE_LOCAL",
  "crs": { "type": "name", "properties": { "name": "EPSG:3857" } } // Optional, implied by coordinateSpace
}
```

---

## 5. Entity-Geometry Contract

This table defines the **Authoritative Mapping** between Domain Entities, permitted Geometry Primitives, Coordinate Spaces, and Semantic Roles.

| Entity (Domain Concept) | Permitted Primitives | Coordinate Space | Semantic Role | Topology Constraints |
| :--- | :--- | :--- | :--- | :--- |
| **Camera** | `Point` (Optical Center), `Polygon` (FOV Cone Projection) | `VENUE_LOCAL` | Reference Frame Anchor | FOV Polygon **MUST** be convex. |
| **Zone** (Generic) | `Polygon` | `VENUE_LOCAL` | Area Semantics (Revenue, Ops) | Simple Polygon. `Area > ε`. |
| **QueueZone** | `Polygon` | `VENUE_LOCAL` | Queueing Area | **MUST** contain `QueueAnchor` (Point) relation. |
| **Table** | `Polygon` | `VENUE_LOCAL` | Seating Asset | **MUST** have `capacity` attribute. Centroid ∈ Polygon. |
| **PointOfInterest (POI)** | `Point` | `VENUE_LOCAL` | Semantic Anchor (Bar, Exit) | N/A |
| **Corridor** | `Polygon` | `VENUE_LOCAL` | Transition Space | **MUST** be simply connected. |
| **ExclusionZone** | `Polygon` | `VENUE_LOCAL` | Negative Space (Obstacles) | `OverlapPolicy: EXCLUDE` enforced. |
| **PrivacyMask** | `Polygon` | `IMAGE_NORMALIZED` | Sensor Blind Spot | **Highest Priority**. Immutable per Camera Version. |
| **HeatmapGrid** | `Polygon` (Grid Cell) | `VENUE_LOCAL` | Aggregation Bucket | Regular tessellation (Square/Hex). |
| **CameraIntrinsicROI** | `Polygon` | `IMAGE_NORMALIZED` | Active Sensor Region | Subset of Unit Square. |

> **Note**: `LINE_STRING` is reserved for future `PathfindingGraph` edges (Task 10.6+). Not valid for current Zone entities.

---

## 6. Validation Framework: The 5-Layer Pipeline

Validation is a **pipeline**. A failure at Layer *N* halts processing; Layer *N+1* is never reached.

### Layer 1: Schema Validation (Contract Layer)
*   **Trigger**: API Ingress (REST/gRPC), Message Consumption.
*   **Mechanism**: JSON Schema (Draft 2020-12) / Protobuf Validation.
*   **Checks**:
    *   GeoJSON `type` ∈ {Point, LineString, Polygon}.
    *   `coordinateSpace` ∈ {IMAGE_NORMALIZED, VENUE_LOCAL}.
    *   Coordinate array depth matches type.
    *   Polygon Closure: `coordinates[0][0] == coordinates[0][N-1]` (within `ε`).
    *   Polygon Vertex Count ≥ 4.
    *   `IMAGE_NORMALIZED` bounds: `∀ c ∈ coords: 0.0 ≤ c ≤ 1.0`.

### Layer 2: Coordinate Space Validation (Domain Layer)
*   **Trigger**: Domain Service `GeometryValidator.validate(geometry, context)`.
*   **Checks**:
    *   **Camera Context**: `IMAGE_NORMALIZED` geometry validated against `Camera.activeResolution` at `ConfigurationVersion`.
    *   **CRS Context**: `VENUE_LOCAL` geometry validated against `Venue.crsDefinition`.
    *   **Precision Clamping**: Coordinates rounded to 6 decimal places (Normalized) / 3 decimal places (Metric).

### Layer 3: Geometric Validity (Topology Layer)
*   **Trigger**: Domain Service / Database Constraint (`CHECK (ST_IsValid(geom))`).
*   **Mechanism**: PostGIS `ST_IsValid`, `ST_IsSimple`, `ST_Area`.
*   **Checks**:
    *   `ST_IsValid(geom) = TRUE`.
    *   `ST_IsSimple(geom) = TRUE` (No self-intersections).
    *   `ST_Area(geom) > ε` (Non-degenerate).
    *   **Winding Order**: Exterior Ring **MUST** be Counter-Clockwise (CCW) for `VENUE_LOCAL` (PostGIS Standard).

### Layer 4: Spatial Relationship Validation (Relational Layer)
*   **Trigger**: Domain Service `SpatialPolicyEngine.evaluate(proposed, existingSet)`.
*   **Mechanism**: PostGIS Spatial Predicates (`ST_Intersects`, `ST_Contains`, `ST_Covers`, `ST_Touches`, `ST_Overlaps`, `ST_Disjoint`, `ST_Within`).
*   **Logic**: Evaluates **Overlap Policy Matrix** (Task 10.5).
    *   *Example*: `Proposed: Table` ∩ `Existing: ExclusionZone` → **REJECT** (Policy: EXCLUDE).
    *   *Example*: `Proposed: QueueZone` ∩ `Existing: Corridor` → **ALLOW** (Policy: SHARED).

### Layer 5: Business Policy Validation (Application Layer)
*   **Trigger**: Aggregate Root / Saga Orchestrator.
*   **Checks**:
    *   Capacity Limits (e.g., `Zone.maxOccupancy` vs `Table.sum(capacity)`).
    *   Regulatory Compliance (Fire egress width, ADA path width).
    *   Privacy Mask Coverage: `PrivacyMask` **MUST** cover `ST_Buffer(Camera.lensPosition, r_min)`.

---

## 7. Error Taxonomy & Structured Response

All validation failures **MUST** return a standardized `GeometryErrorResponse` (RFC 9457 `application/problem+json` compatible).

### 7.1 Error Code Namespace: `GEO-<LAYER>-<CODE>`
| Layer | Prefix | HTTP Status | Description |
| :--- | :--- | :--- | :--- |
| Schema | `GEO-SCHEMA` | `400 Bad Request` | Malformed GeoJSON, missing fields, type mismatch. |
| Coordinate Space | `GEO-CS` | `400 Bad Request` | Out of bounds, missing calibration, CRS mismatch. |
| Topology | `GEO-TOPO` | `422 Unprocessable Entity` | Invalid geometry, self-intersection, degenerate area, winding order. |
| Spatial Policy | `GEO-POLICY` | `409 Conflict` | Overlap Policy violation (Exclusion, Privacy, Capacity). |
| Business Policy | `GEO-BIZ` | `409 Conflict` | Regulatory, Capacity, Business Rule violation. |
| System | `GEO-SYS` | `500 Internal Server Error` | PostGIS exception, Projection failure, Version conflict. |

### 7.2 Response Payload Structure
```json
{
  "type": "https://api.hotelops.ai/errors/geometry-validation-failed",
  "title": "Geometry Validation Failed",
  "status": 422,
  "traceId": "01HXXXXXXXXXXXXXXXXXXXXXXX",
  "errors": [
    {
      "code": "GEO-TOPO-003",
      "detail": "Polygon exterior ring is not closed (start != end).",
      "pointer": "/geometry/coordinates/0",
      "context": {
        "expected": "Closed Ring (N>=4, Start=End)",
        "actual": "Open Ring (N=3)",
        "coordinateSpace": "VENUE_LOCAL",
        "entityId": "zone-123",
        "entityType": "QueueZone"
      }
    },
    {
      "code": "GEO-POLICY-001",
      "detail": "Proposed geometry intersects ExclusionZone 'column-4' (Policy: EXCLUDE).",
      "pointer": "/geometry",
      "context": {
        "policy": "EXCLUDE",
        "conflictingEntityId": "excl-4",
        "conflictingEntityType": "ExclusionZone",
        "intersectionArea": 0.52,
        "intersectionAreaUnit": "sq_meters"
      }
    }
  ]
}
```

---

## 8. Architectural Invariants (The "GEO-" Constraints)

These are **System Invariants** enforced by Database Constraints, Domain Services, and Integration Tests.

| Invariant ID | Formal Statement | Enforcement Point |
| :--- | :--- | :--- |
| **INV-GEO-01** | `∀ g ∈ Geometries: g.coordinateSpace ∈ {IMAGE_NORMALIZED, VENUE_LOCAL}` | DB `CHECK`, Schema Validation |
| **INV-GEO-02** | `∀ g ∈ Geometries: ST_IsValid(g.geom) = TRUE` | DB `CHECK (ST_IsValid(geom))`, Layer 3 |
| **INV-GEO-03** | `∀ g_img ∈ Geometries | g_img.coordinateSpace = IMAGE_NORMALIZED: ∀ c ∈ g_img.coordinates: 0.0 ≤ c ≤ 1.0` | Layer 1, Layer 2 |
| **INV-GEO-04** | `∀ g_venue ∈ Geometries | g_venue.coordinateSpace = VENUE_LOCAL: g_venue.geom && ST_SRID(g_venue.geom) = Venue.SRID` | DB Constraint, Layer 2 |
| **INV-GEO-05** | `Versioning: Geometry.configurationVersion_id = Entity.configurationVersion_id` | DB Foreign Key, Aggregate Root |
| **INV-GEO-06** | `Cross-Version Reference: ¬∃ g1, g2: g1.configVersion ≠ g2.configVersion ∧ ST_Intersects(g1, g_2)` | **Application Layer Only** (DB cannot enforce cross-version easily). |
| **INV-GEO-07** | `Privacy Precedence: PrivacyMask ⊑ ExclusionZone ⊑ StandardZone` (Where `⊑` denotes "Overrides Policy Of") | Layer 4 Policy Engine |
| **INV-GEO-08** | `Immutability: UPDATE geometries SET geom = ... WHERE id = ...` **MUST** return 0 rows affected. | DB Trigger / RLS Policy / Application Forbid. |
| **INV-GEO-09** | `Projection Determinism: Project(ImageNorm, H) = VenueLocal` is pure function. Same `H` + Same Input = Same Output. | Unit Tests, CI Pipeline. |
| **INV-GEO-10** | `No GeometryCollections: Geometry.type ≠ 'GeometryCollection'` | Schema Validation (Layer 1). |

---

## 9. Implementation Strategy

### 9.1 Technology Stack
*   **Database**: PostgreSQL 16+ with **PostGIS 3.5+**.
*   **Spatial Index**: `GIST` (Generalized Search Tree) on `geometry` column.
*   **SRID Management**: Custom SRIDs for `VENUE_LOCAL` (e.g., 900000+ range) registered in `spatial_ref_sys`. `IMAGE_NORMALIZED` uses SRID 0 (Cartesian) or 4326 (if treated as lon/lat), but **logic treats as Unit Square**.
*   **ORM**: Prisma / SQLAlchemy / Drizzle with raw SQL escape hatches for `ST_*` functions.

### 9.2 Schema Definition (Conceptual)
```sql
-- Domain Table
CREATE TABLE spatial_geometries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id       UUID NOT NULL,                   -- FK to Zone, Camera, Table, etc.
    entity_type     VARCHAR(50) NOT NULL,            -- Discriminator: 'Zone', 'Table', 'PrivacyMask'
    configuration_version_id UUID NOT NULL,          -- FK to ConfigurationVersion (Partition Key)
    coordinate_space VARCHAR(20) NOT NULL CHECK (coordinate_space IN ('IMAGE_NORMALIZED', 'VENUE_LOCAL')),
    -- PostGIS Geometry Column (Planar for VENUE_LOCAL, Unit Square for IMAGE_NORMALIZED)
    geom            GEOMETRY(GEOMETRY, 0) NOT NULL,  -- SRID 0 for generic; specific SRID for VENUE_LOCAL via CHECK
    -- Metadata
    properties      JSONB DEFAULT '{}',              -- Semantic attributes (capacity, color, etc.)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Constraints
    CONSTRAINT chk_valid_geom CHECK (ST_IsValid(geom)),
    CONSTRAINT chk_simple_geom CHECK (ST_IsSimple(geom)),
    CONSTRAINT chk_venue_srid CHECK (
        coordinate_space = 'IMAGE_NORMALIZED' OR 
        ST_SRID(geom) = (SELECT srid FROM venues WHERE id = current_venue_id()) -- Simplified
    ),
    -- Partitioning by ConfigurationVersion for Immutable History & Query Perf
) PARTITION BY LIST (configuration_version_id);

-- Indexing
CREATE INDEX idx_spatial_geom_gist ON spatial_geometries USING GIST (geom);
CREATE INDEX idx_spatial_entity_version ON spatial_geometries (entity_id, configuration_version_id);
```

### 9.3 Separation of Concerns

| Layer | Responsibility | Technology |
| :--- | :--- | :--- |
| **API Layer** | Schema Validation (Layer 1), DTO Mapping, Error Serialization (RFC 9457). | FastAPI / NestJS / Go Chi + JSON Schema Validator. |
| **Domain Service** | Coordinate Space Logic (Layer 2), Policy Engine (Layer 4), Business Rules (Layer 5), Projection Math (`H` matrix). | Pure Python/TypeScript/Rust. **Zero DB Dependencies.** Unit tested with property-based testing (Hypothesis/fast-check). |
| **Repository / Persistence** | PostGIS Interaction (Layer 3 execution), Transaction Management, Partition Routing. | SQLAlchemy / Prisma / Raw SQL. |
| **Database** | Physical Storage, Spatial Indexing, Hard Constraints (Layer 3), Immutability Enforcement. | PostgreSQL + PostGIS. |

---

## 10. Future Extensibility (Non-Goals for Task 10.4)

1.  **3D Volumes (`PolyhedralSurface`, `TIN`, `Solid`)**: Deferred to Task 10.8 (Multi-Floor Volumetric Analytics).
2.  **Topology Network (`LineString` Graph)**: Deferred to Task 10.6 (Pathfinding/Navigation Mesh).
3.  **Dynamic/Time-Series Geometry**: Deferred to Task 11.x (Real-time Occupancy Polygons).
4.  **Interior Rings (Holes in Polygons)**: Explicitly modeled as `ExclusionZone` entities with `OverlapPolicy: EXCLUDE` to maintain simple polygon primitives and explicit policy semantics.

---

## 11. Acceptance Criteria for "Contract Frozen"

1.  [ ] This ADR is ratified by Architecture Review Board.
2.  [ ] JSON Schemas for `GeometryInput` and `GeometryErrorResponse` published to Schema Registry.
3.  [ ] PostGIS Migration Script (`V10.4.0__spatial_geometry_model.sql`) reviewed and approved.
4.  [ ] Property-Based Test Suite for `DomainService.GeometryValidator` achieves 100% coverage on Layer 1-3 logic.
5.  [ ] Integration Test Suite validates Layer 4 Policy Matrix against 50+ synthetic spatial scenarios.
6.  [ ] OpenAPI Spec updated with `Geometry` component schemas.