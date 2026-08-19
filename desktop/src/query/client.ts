/**
 * Query Client Configuration - Centralized server-state cache configuration.
 *
 * This module configures TanStack Query (React Query) with conservative defaults
 * appropriate for a desktop application communicating with FastAPI backend.
 *
 * Architecture:
 *   Server-State Cache
 *       ↓
 *   Typed API Services
 *       ↓
 *   Typed HTTP Client
 *       ↓
 *   FastAPI
 */

import { QueryClient } from "@tanstack/react-query";
import type { ApiError } from "@/api/types";

/**
 * Retry policy based on error type.
 *
 * Does NOT automatically retry:
 * - 400 (Bad Request)
 * - 401 (Unauthorized)
 * - 403 (Forbidden)
 * - 404 (Not Found)
 * - 409 (Conflict)
 * - 422 (Validation Error)
 *
 * Potentially retries:
 * - Network failures
 * - 5xx errors
 * - 502 (Bad Gateway)
 * - 503 (Service Unavailable)
 * - Transient timeouts
 */
function retryPolicy(failureCount: number, error: unknown): boolean {
  // Don't retry more than 3 times
  if (failureCount >= 3) {
    return false;
  }

  // Check if it's a normalized API error
  if (error && typeof error === "object" && "status" in error) {
    const apiError = error as ApiError;
    const status = apiError.status;

    // Never retry these status codes
    if (
      status === 400 ||
      status === 401 ||
      status === 403 ||
      status === 404 ||
      status === 409 ||
      status === 422
    ) {
      return false;
    }

    // Retry on network errors, 5xx, 429, 502, 503
    if (
      status === 0 || // Network error
      status === 429 || // Rate limited
      status === 500 ||
      status === 502 ||
      status === 503 ||
      status === 504
    ) {
      return true;
    }
  }

  // For network errors (TypeError from fetch)
  if (error instanceof TypeError && error.message.includes("fetch")) {
    return true;
  }

  // Don't retry unknown errors
  return false;
}

/**
 * Stale time configuration based on data category.
 *
 * Categories:
 * - FAST-CHANGING: operational status, active alerts, camera health (30s)
 * - MODERATE: event history, evidence metadata, operational summaries (2min)
 * - SLOW: configuration, user profile, venue configuration, permissions (10min)
 */
export const staleTimeConfig = {
  fast: 30_000, // 30 seconds
  moderate: 120_000, // 2 minutes
  slow: 600_000, // 10 minutes
} as const;

/**
 * Create the centralized QueryClient with conservative defaults.
 *
 * Defaults:
 * - staleTime: 2 minutes (moderate)
 * - gcTime: 5 minutes (garbage collection)
 * - retry: custom retry policy based on error type
 * - refetchOnWindowFocus: false (desktop app, user controls refresh)
 * - refetchOnReconnect: true
 * - refetchOnMount: true
 */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // Conservative stale time - data is fresh for 2 minutes by default
        staleTime: staleTimeConfig.moderate,
        // Keep in cache for 5 minutes after last observer unmounts
        gcTime: 300_000,
        // Custom retry policy
        retry: retryPolicy,
        // Don't refetch on window focus (desktop app)
        refetchOnWindowFocus: false,
        // Refetch on reconnect (network recovery)
        refetchOnReconnect: true,
        // Refetch on mount if data is stale
        refetchOnMount: "always",
        // Don't throw errors, let components handle them
        throwOnError: false,
        // Structural sharing to prevent unnecessary re-renders
        structuralSharing: true,
      },
      mutations: {
        // Don't throw errors, let components handle them
        throwOnError: false,
        // Retry mutations only for network errors
        retry: (failureCount, error) => {
          if (failureCount >= 2) return false;
          if (error instanceof TypeError && error.message.includes("fetch")) {
            return true;
          }
          return false;
        },
      },
    },
  });
}

/**
 * Default QueryClient instance.
 * Created once at module load time.
 */
export const queryClient = createQueryClient();

export { queryClient as default };
