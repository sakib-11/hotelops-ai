/**
 * Operational Query Hooks - Server-state cache for the vertical slice.
 *
 * Typed, cached access to the authorized operational retrieval surface
 * (Task 18.13). The desktop reads the occupancy event/fact and the
 * server-derived evidence availability through these hooks — it never
 * computes occupancy or evidence state itself.
 *
 * Stale time: moderate (2 min) — an occupancy event/fact is immutable
 * once persisted; evidence availability can change as the worker
 * processes requests, so it refreshes a bit sooner.
 */

import { useQuery, type UseQueryOptions, type UseQueryResult } from "@tanstack/react-query";
import { queryKeys } from "../keys";
import { staleTimeConfig } from "../client";
import {
  getOperationalEvent,
  getOperationalFact,
  getEvidenceAvailability,
} from "@/api/services/operational";
import type {
  OccupancyEventResponse,
  OccupancyFactResponse,
  EvidenceAvailabilityResponse,
} from "@/api/types/operational";

/**
 * Hook for one occupancy_session domain event.
 */
export function useOperationalEvent(
  eventId: string,
  options?: Partial<
    UseQueryOptions<
      OccupancyEventResponse,
      Error,
      OccupancyEventResponse,
      ReturnType<typeof queryKeys.operational.event>
    >
  >,
): UseQueryResult<OccupancyEventResponse> {
  return useQuery({
    queryKey: queryKeys.operational.event(eventId),
    queryFn: () => getOperationalEvent(eventId),
    staleTime: staleTimeConfig.moderate,
    // A missing/out-of-scope event (404) is a legitimate "empty" state —
    // never retried.
    retry: false,
    ...options,
  });
}

/**
 * Hook for one occupancy_snapshot business fact.
 */
export function useOperationalFact(
  factId: string,
  options?: Partial<
    UseQueryOptions<
      OccupancyFactResponse,
      Error,
      OccupancyFactResponse,
      ReturnType<typeof queryKeys.operational.fact>
    >
  >,
): UseQueryResult<OccupancyFactResponse> {
  return useQuery({
    queryKey: queryKeys.operational.fact(factId),
    queryFn: () => getOperationalFact(factId),
    staleTime: staleTimeConfig.moderate,
    retry: false,
    ...options,
  });
}

/**
 * Hook for the server-derived evidence availability of one event.
 */
export function useEvidenceAvailability(
  eventId: string,
  options?: Partial<
    UseQueryOptions<
      EvidenceAvailabilityResponse,
      Error,
      EvidenceAvailabilityResponse,
      ReturnType<typeof queryKeys.operational.eventEvidence>
    >
  >,
): UseQueryResult<EvidenceAvailabilityResponse> {
  return useQuery({
    queryKey: queryKeys.operational.eventEvidence(eventId),
    queryFn: () => getEvidenceAvailability(eventId),
    staleTime: staleTimeConfig.fast,
    retry: false,
    ...options,
  });
}
