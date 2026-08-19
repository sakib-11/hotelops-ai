/**
 * Health Query Hooks - Server-state cache for health endpoints.
 *
 * Provides typed, cached access to health and readiness endpoints.
 * Uses moderate stale time (2 min) as health status doesn't change rapidly.
 */

import { useQuery, type UseQueryOptions, type UseQueryResult } from "@tanstack/react-query";
import { queryClient } from "../client";
import { queryKeys } from "../keys";
import {
  getHealth,
  getReadiness,
  isApiReady,
  type HealthResponse,
  type ReadinessResponse,
} from "@/api/services/health";

/**
 * Hook for health check (liveness probe).
 *
 * Returns 200 if the application process is alive.
 * Does NOT check external dependencies.
 *
 * Stale time: moderate (2 min) - process status doesn't change frequently.
 */
export function useHealth(
  options?: Partial<
    UseQueryOptions<
      HealthResponse,
      Error,
      HealthResponse,
      ReturnType<typeof queryKeys.health.health>
    >
  >,
): UseQueryResult<HealthResponse> {
  return useQuery({
    queryKey: queryKeys.health.health(),
    queryFn: getHealth,
    staleTime: 120_000, // 2 minutes - process health doesn't change often
    ...options,
  });
}

/**
 * Hook for readiness check (dependency check).
 *
 * Returns 200 with status=ready when all dependencies are healthy.
 * Returns 503 with status=not_ready when any dependency is unavailable.
 *
 * Stale time: fast (30s) - dependency status can change.
 */
export function useReadiness(
  options?: Partial<
    UseQueryOptions<
      ReadinessResponse,
      Error,
      ReadinessResponse,
      ReturnType<typeof queryKeys.health.readiness>
    >
  >,
): UseQueryResult<ReadinessResponse> {
  return useQuery({
    queryKey: queryKeys.health.readiness(),
    queryFn: getReadiness,
    staleTime: 30_000, // 30 seconds - dependency status can change
    ...options,
  });
}

/**
 * Hook for API version info.
 *
 * Stale time: slow (10 min) - version rarely changes.
 */
export function useApiVersion(
  options?: Partial<
    UseQueryOptions<
      { service: string; version: string } | null,
      Error,
      { service: string; version: string } | null,
      ReturnType<typeof queryKeys.health.version>
    >
  >,
): UseQueryResult<{ service: string; version: string } | null> {
  return useQuery({
    queryKey: queryKeys.health.version(),
    queryFn: async () => {
      const response = await import("@/api/services/health").then((m) => m.getApiVersion());
      return response;
    },
    staleTime: 600_000, // 10 minutes - version rarely changes
    ...options,
  });
}

/**
 * Hook for combined health and readiness check.
 *
 * Returns true if both health and readiness are ok.
 * Useful for startup checks and connection validation.
 *
 * Stale time: fast (30s) - reflects readiness status.
 */
export function useIsApiReady(
  options?: Partial<
    UseQueryOptions<boolean, Error, boolean, ReturnType<typeof queryKeys.health.ping>>
  >,
): UseQueryResult<boolean> {
  return useQuery({
    queryKey: queryKeys.health.ping(),
    queryFn: isApiReady,
    staleTime: 30_000, // 30 seconds
    ...options,
  });
}

/**
 * Prefetch health data for faster initial load.
 * Useful for startup sequence.
 */
export async function prefetchHealth(): Promise<void> {
  await queryClient.prefetchQuery({
    queryKey: queryKeys.health.health(),
    queryFn: getHealth,
    staleTime: 120_000,
  });
}

/**
 * Prefetch readiness data.
 */
export async function prefetchReadiness(): Promise<void> {
  await queryClient.prefetchQuery({
    queryKey: queryKeys.health.readiness(),
    queryFn: getReadiness,
    staleTime: 30_000,
  });
}

/**
 * Invalidate all health queries.
 * Useful after known infrastructure changes.
 */
export function invalidateHealthQueries(): void {
  queryClient.invalidateQueries({ queryKey: queryKeys.health.all() });
}

/**
 * Set health data directly in cache (e.g., after WebSocket update).
 */
export function setHealthData(health: import("@/api/types").HealthResponse): void {
  queryClient.setQueryData(queryKeys.health.health(), health);
}

/**
 * Set readiness data directly in cache (e.g., after WebSocket update).
 */
export function setReadinessData(readiness: import("@/api/types").ReadinessResponse): void {
  queryClient.setQueryData(queryKeys.health.readiness(), readiness);
}
