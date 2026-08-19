/**
 * Operational Service - API service for the operational vertical slice.
 *
 * Typed access to the authorized retrieval surface (Task 18.12/18.13):
 *
 *   GET /operational/events/{event_id}              → OccupancyEventResponse
 *   GET /operational/facts/{fact_id}                → OccupancyFactResponse
 *   GET /operational/events/{event_id}/evidence     → EvidenceAvailabilityResponse
 *
 * Authorization (authenticated actor, tenant/venue scope, RLS) is
 * enforced entirely by FastAPI — the desktop never sends a tenant or
 * venue, and never derives occupancy/evidence state locally.
 */

import { httpClient } from "../client";
import type {
  OccupancyEventResponse,
  OccupancyFactResponse,
  EvidenceAvailabilityResponse,
} from "../types/operational";

/**
 * Retrieve one occupancy_session domain event (Task 16) by identity.
 */
export async function getOperationalEvent(eventId: string): Promise<OccupancyEventResponse> {
  const response = await httpClient.get<OccupancyEventResponse>(
    `/operational/events/${encodeURIComponent(eventId)}`,
  );
  return response.data;
}

/**
 * Retrieve one occupancy_snapshot business fact (Task 15) by identity.
 */
export async function getOperationalFact(factId: string): Promise<OccupancyFactResponse> {
  const response = await httpClient.get<OccupancyFactResponse>(
    `/operational/facts/${encodeURIComponent(factId)}`,
  );
  return response.data;
}

/**
 * Retrieve whether evidence exists for one operational event.
 *
 * The answer is computed server-side (the durable Task 18.9 linkage) —
 * the desktop displays it as-is.
 */
export async function getEvidenceAvailability(
  eventId: string,
): Promise<EvidenceAvailabilityResponse> {
  const response = await httpClient.get<EvidenceAvailabilityResponse>(
    `/operational/events/${encodeURIComponent(eventId)}/evidence`,
  );
  return response.data;
}
