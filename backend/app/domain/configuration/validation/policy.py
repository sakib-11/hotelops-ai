"""Centralized spatial policy registry (Task 10.7).

The SpatialPolicyRegistry is the SINGLE place that defines the semantic
meaning of spatial relationships between configuration entities:

  - which entity pairs may meaningfully overlap and with what severity
  - which containment relationships are REQUIRED when declared
  - boundary touching vs meaningful area overlap
  - privacy/exclusion precedence

Individual models and validators NEVER hard-code spatial rules — they
ask the registry. This keeps policy deterministic and auditable.

Policies:

  table-table             REJECT      (blocking error)
  table->zone containment REQUIRED    (when a parent zone is declared)
  queue->zone containment REQUIRED    (when a parent zone is declared)
  service->zone contain.  REQUIRED    (when a parent zone is declared)
  entrance->zone          ALLOW touch (entrance may touch/cross boundary)
  zone-zone               ALLOW       (shared/semantically valid)
  queue-service           ALLOW       (queue at a service counter)
  queue-queue             ALLOW       (parallel queues may touch)
  privacy-privacy         REJECT when contradictory (same camera, diff action)
  exclusion-exclusion     ALLOW
  privacy-exclusion       ALLOW when consistent; REJECT contradictions

Privacy precedence (fixed, deterministic):
    PrivacyROI  >  ExclusionROI  >  standard zones/tables/entrances
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from contracts.geometry import OverlapPolicy

# Documented, deterministic tolerance shared with the spatial math.
AREA_TOLERANCE = 1e-6


class EntityKind(StrEnum):
    CAMERA = "camera"
    ZONE = "zone"
    TABLE = "table"
    ENTRANCE = "entrance"
    QUEUE_AREA = "queue_area"
    SERVICE_AREA = "service_area"
    PRIVACY_ROI = "privacy_roi"
    EXCLUSION_ROI = "exclusion_roi"


@dataclass(frozen=True)
class RelationshipPolicy:
    """Policy for one (kind_a, kind_b) spatial relationship."""

    kind_a: EntityKind
    kind_b: EntityKind
    overlap: OverlapPolicy  # ALLOW | REJECT | VALIDATE
    severity: str  # "error" | "warning"
    # When True, if entity A declares a parent of kind B (e.g. a table
    # declaring its zone), the parent MUST contain the child.
    requires_containment: bool = False
    # When True, boundary touching is explicitly permitted even when
    # meaningful overlap is REJECTed (entrance <-> zone).
    permits_boundary_touch: bool = False
    note: str = ""


@dataclass(frozen=True)
class SpatialPolicyRegistry:
    """Deterministic registry of spatial relationship policies.

    Policies are indexed by the unordered pair (kind_a, kind_b) so
    lookups are symmetric and deterministic.
    """

    _policies: dict[frozenset[EntityKind], RelationshipPolicy] = field(default_factory=dict)

    @classmethod
    def default(cls) -> SpatialPolicyRegistry:
        """The authoritative default policy set (Task 10.7)."""
        policies = [
            # --- Table overlap: blocking ---
            RelationshipPolicy(
                kind_a=EntityKind.TABLE,
                kind_b=EntityKind.TABLE,
                overlap=OverlapPolicy.REJECT,
                severity="error",
                note="Meaningful table-table overlap is a blocking error.",
            ),
            # --- Containment requirements (declared parent must contain) ---
            RelationshipPolicy(
                kind_a=EntityKind.TABLE,
                kind_b=EntityKind.ZONE,
                overlap=OverlapPolicy.VALIDATE,
                severity="error",
                requires_containment=True,
                note="A table declaring a parent zone must be contained by it.",
            ),
            RelationshipPolicy(
                kind_a=EntityKind.QUEUE_AREA,
                kind_b=EntityKind.ZONE,
                overlap=OverlapPolicy.VALIDATE,
                severity="error",
                requires_containment=True,
                note="A queue area declaring a parent zone must be contained by it.",
            ),
            RelationshipPolicy(
                kind_a=EntityKind.SERVICE_AREA,
                kind_b=EntityKind.ZONE,
                overlap=OverlapPolicy.VALIDATE,
                severity="error",
                requires_containment=True,
                note="A service area declaring a parent zone must be contained by it.",
            ),
            # --- Entrance may touch/cross a zone boundary ---
            RelationshipPolicy(
                kind_a=EntityKind.ENTRANCE,
                kind_b=EntityKind.ZONE,
                overlap=OverlapPolicy.ALLOW,
                severity="error",
                permits_boundary_touch=True,
                note="Entrance geometry may touch or cross a zone boundary.",
            ),
            # --- Zone-zone: allowed per zone-type semantics ---
            RelationshipPolicy(
                kind_a=EntityKind.ZONE,
                kind_b=EntityKind.ZONE,
                overlap=OverlapPolicy.ALLOW,
                severity="warning",
                note="Adjacent zones may share boundaries; meaningful overlap is "
                "evaluated per zone-type policy.",
            ),
            # --- Queue/service: allowed (queue forms at the counter) ---
            RelationshipPolicy(
                kind_a=EntityKind.QUEUE_AREA,
                kind_b=EntityKind.SERVICE_AREA,
                overlap=OverlapPolicy.ALLOW,
                severity="warning",
                note="A queue may adjoin its service counter.",
            ),
            # --- Queue-queue: parallel queues may touch ---
            RelationshipPolicy(
                kind_a=EntityKind.QUEUE_AREA,
                kind_b=EntityKind.QUEUE_AREA,
                overlap=OverlapPolicy.ALLOW,
                severity="warning",
                note="Parallel queues may share a boundary.",
            ),
            # --- Privacy vs everything: privacy always wins ---
            RelationshipPolicy(
                kind_a=EntityKind.PRIVACY_ROI,
                kind_b=EntityKind.ZONE,
                overlap=OverlapPolicy.ALLOW,
                severity="error",
                note="Privacy ROI may cover any zone; privacy action applies.",
            ),
            RelationshipPolicy(
                kind_a=EntityKind.PRIVACY_ROI,
                kind_b=EntityKind.TABLE,
                overlap=OverlapPolicy.ALLOW,
                severity="error",
                note="Privacy ROI may cover tables.",
            ),
            RelationshipPolicy(
                kind_a=EntityKind.PRIVACY_ROI,
                kind_b=EntityKind.QUEUE_AREA,
                overlap=OverlapPolicy.ALLOW,
                severity="error",
            ),
            RelationshipPolicy(
                kind_a=EntityKind.PRIVACY_ROI,
                kind_b=EntityKind.SERVICE_AREA,
                overlap=OverlapPolicy.ALLOW,
                severity="error",
            ),
            RelationshipPolicy(
                kind_a=EntityKind.PRIVACY_ROI,
                kind_b=EntityKind.ENTRANCE,
                overlap=OverlapPolicy.ALLOW,
                severity="error",
            ),
            # --- Exclusion: capability-specific, never auto-recording-delete ---
            RelationshipPolicy(
                kind_a=EntityKind.EXCLUSION_ROI,
                kind_b=EntityKind.ZONE,
                overlap=OverlapPolicy.ALLOW,
                severity="error",
                note="Exclusion ROI restricts CV tasks, not recording.",
            ),
            RelationshipPolicy(
                kind_a=EntityKind.EXCLUSION_ROI,
                kind_b=EntityKind.TABLE,
                overlap=OverlapPolicy.ALLOW,
                severity="error",
            ),
            RelationshipPolicy(
                kind_a=EntityKind.EXCLUSION_ROI,
                kind_b=EntityKind.QUEUE_AREA,
                overlap=OverlapPolicy.ALLOW,
                severity="error",
            ),
            RelationshipPolicy(
                kind_a=EntityKind.EXCLUSION_ROI,
                kind_b=EntityKind.SERVICE_AREA,
                overlap=OverlapPolicy.ALLOW,
                severity="error",
            ),
            RelationshipPolicy(
                kind_a=EntityKind.EXCLUSION_ROI,
                kind_b=EntityKind.ENTRANCE,
                overlap=OverlapPolicy.ALLOW,
                severity="error",
            ),
            # --- Privacy <-> exclusion: consistent overlap allowed ---
            RelationshipPolicy(
                kind_a=EntityKind.PRIVACY_ROI,
                kind_b=EntityKind.EXCLUSION_ROI,
                overlap=OverlapPolicy.VALIDATE,
                severity="error",
                note="Overlapping privacy/exclusion regions allowed when semantically "
                "consistent; contradictions are rejected.",
            ),
            # --- Cameras: no spatial overlap semantics (points) ---
            RelationshipPolicy(
                kind_a=EntityKind.CAMERA,
                kind_b=EntityKind.ZONE,
                overlap=OverlapPolicy.ALLOW,
                severity="warning",
            ),
        ]
        registry = cls()
        for p in policies:
            registry.register(p)
        return registry

    def register(self, policy: RelationshipPolicy) -> None:
        key = frozenset({policy.kind_a, policy.kind_b})
        if key in self._policies:
            msg = f"Duplicate spatial policy for {policy.kind_a}-{policy.kind_b}"
            raise ValueError(msg)
        self._policies[key] = policy

    def policy_for(self, kind_a: EntityKind, kind_b: EntityKind) -> RelationshipPolicy | None:
        """Return the policy for an unordered entity-kind pair."""
        return self._policies.get(frozenset({kind_a, kind_b}))

    def requires_containment(self, kind_a: EntityKind, kind_b: EntityKind) -> bool:
        policy = self.policy_for(kind_a, kind_b)
        return bool(policy and policy.requires_containment)

    def rejects_overlap(self, kind_a: EntityKind, kind_b: EntityKind) -> bool:
        policy = self.policy_for(kind_a, kind_b)
        return bool(policy and policy.overlap == OverlapPolicy.REJECT)

    def permits_boundary_touch(self, kind_a: EntityKind, kind_b: EntityKind) -> bool:
        policy = self.policy_for(kind_a, kind_b)
        return bool(policy and policy.permits_boundary_touch)

    def severity_for(self, kind_a: EntityKind, kind_b: EntityKind) -> str:
        policy = self.policy_for(kind_a, kind_b)
        return policy.severity if policy else "error"


# Singleton default policy registry — deterministic and shared.
DEFAULT_SPATIAL_POLICY_REGISTRY = SpatialPolicyRegistry.default()

__all__ = [
    "AREA_TOLERANCE",
    "DEFAULT_SPATIAL_POLICY_REGISTRY",
    "EntityKind",
    "RelationshipPolicy",
    "SpatialPolicyRegistry",
]
