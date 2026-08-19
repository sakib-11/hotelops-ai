/**
 * Operational Vertical-Slice Types (Task 18.13)
 *
 * Mirrors the backend canonical response DTOs (contracts/operational):
 * the occupancy event/fact retrieval surface (Task 18.12) plus the
 * evidence-availability answer (Task 18.13).
 *
 * These are the ONLY wire shapes the desktop may consume from the
 * operational API — internal backend ORM rows are never exposed. The
 * desktop NEVER derives occupancy or evidence state; it renders exactly
 * what FastAPI returns through the authorized retrieval surface
 * (Tauri → API client → FastAPI → authorized repository → PostgreSQL).
 *
 * Do not invent types that don't exist in the backend.
 */

// =============================================================================
// Occupancy domain event (Task 16 / 18.12)
// =============================================================================

export type OccupancySessionPhase = "started" | "ended";

/** The canonical Task 16 occupancy_session payload — verbatim. */
export interface OccupancySessionPayload {
  schema_version: string;
  phase: OccupancySessionPhase;
  tenant_id: string;
  venue_id: string;
  session_id: string;
  camera_id: string;
  spatial_context_id: string | null;
  occupancy_count: number;
  occupied_tracks: string[];
  occupancy_time: string;
  configuration_version_id: string;
  rule_id: string;
  rule_version: string;
}

/** GET /operational/events/{event_id} → canonical occupancy event DTO. */
export interface OccupancyEventResponse {
  event_id: string;
  event_type: string;
  schema_version: string;
  tenant_id: string;
  venue_id: string;
  session_id: string | null;
  camera_id: string | null;
  event_time: string;
  produced_at: string;
  source: string;
  correlation_id: string | null;
  causation_id: string | null;
  payload: OccupancySessionPayload;
}

// =============================================================================
// Occupancy business fact (Task 15 / 18.12)
// =============================================================================

/** The canonical Task 15 temporal state key — verbatim. */
export interface TemporalStateKey {
  fsm_kind: string;
  tenant_id: string;
  venue_id: string;
  session_id: string;
  camera_id: string;
  configuration_version_id: string;
  track_id: string;
  semantic_context: string | null;
}

/** The canonical Task 15 occupancy snapshot payload — verbatim. */
export interface OccupancySnapshotPayload {
  schema_version: string;
  snapshot_id: string;
  fsm_kind: string;
  key: TemporalStateKey;
  event_time: string;
  previous_count: number;
  delta: number;
  occupancy_count: number;
  occupied_tracks: string[];
  source_transition_id: string;
  fsm_version: string;
  policy_revision: string;
}

/** GET /operational/facts/{fact_id} → canonical occupancy fact DTO. */
export interface OccupancyFactResponse {
  fact_id: string;
  fact_type: string;
  fsm_kind: string;
  schema_version: string;
  tenant_id: string;
  venue_id: string;
  session_id: string | null;
  camera_id: string | null;
  configuration_version_id: string | null;
  event_time: string;
  source_transition_id: string | null;
  fsm_version: string;
  policy_revision: string | null;
  payload: OccupancySnapshotPayload;
}

// =============================================================================
// Evidence availability (Task 18.13) — a server-derived fact
// =============================================================================

/**
 * GET /operational/events/{event_id}/evidence → canonical answer.
 *
 * ``available`` is computed by the backend from the durable Task 18.9
 * event → evidence request linkage. The desktop never derives it.
 */
export interface EvidenceAvailabilityResponse {
  event_id: string;
  available: boolean;
  evidence_ref_id: string | null;
}
