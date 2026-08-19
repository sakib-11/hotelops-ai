/**
 * Query Key Architecture - Centralized query key definitions.
 *
 * All query keys are defined here to ensure:
 * - Deterministic keys
 * - Type safety where practical
 * - Tenant/venue awareness where required
 * - Consistency across the application
 * - Easy invalidation
 *
 * Keys are structured as tuples for TanStack Query's native key hashing.
 */

import type { TenantId, VenueId } from "@/api/types";

/**
 * Base query key factory functions.
 * Each domain gets its own namespace.
 */
export const queryKeys = {
  // Authentication & session
  auth: {
    all: () => ["auth"] as const,
    currentUser: () => ["auth", "current-user"] as const,
    session: () => ["auth", "session"] as const,
  },

  // Health & system
  health: {
    all: () => ["health"] as const,
    health: () => ["health", "health"] as const,
    readiness: () => ["health", "readiness"] as const,
    ping: () => ["health", "ping"] as const,
    version: () => ["health", "version"] as const,
  },

  // Users
  users: {
    all: () => ["users"] as const,
    lists: () => [...queryKeys.users.all(), "list"] as const,
    list: (filters?: Record<string, unknown>) => [...queryKeys.users.lists(), filters] as const,
    detail: (userId: string) => [...queryKeys.users.all(), "detail", userId] as const,
    byTenant: (tenantId: TenantId) => [...queryKeys.users.all(), "tenant", tenantId] as const,
  },

  // Tenants
  tenants: {
    all: () => ["tenants"] as const,
    lists: () => [...queryKeys.tenants.all(), "list"] as const,
    list: (filters?: Record<string, unknown>) => [...queryKeys.tenants.lists(), filters] as const,
    detail: (tenantId: TenantId) => [...queryKeys.tenants.all(), "detail", tenantId] as const,
  },

  // Venues
  venues: {
    all: () => ["venues"] as const,
    lists: () => [...queryKeys.venues.all(), "list"] as const,
    list: (filters?: Record<string, unknown>) => [...queryKeys.venues.lists(), filters] as const,
    detail: (venueId: VenueId) => [...queryKeys.venues.all(), "detail", venueId] as const,
    byTenant: (tenantId: TenantId) => [...queryKeys.venues.all(), "tenant", tenantId] as const,
  },

  // Cameras
  cameras: {
    all: () => ["cameras"] as const,
    lists: () => [...queryKeys.cameras.all(), "list"] as const,
    list: (params?: {
      tenantId?: TenantId;
      venueId?: VenueId;
      filters?: Record<string, unknown>;
    }) => [...queryKeys.cameras.lists(), params] as const,
    detail: (cameraId: string) => [...queryKeys.cameras.all(), "detail", cameraId] as const,
    health: (cameraId: string) => [...queryKeys.cameras.all(), "health", cameraId] as const,
    byVenue: (venueId: VenueId) => [...queryKeys.cameras.all(), "venue", venueId] as const,
    byTenant: (tenantId: TenantId) => [...queryKeys.cameras.all(), "tenant", tenantId] as const,
  },

  // Events
  events: {
    all: () => ["events"] as const,
    lists: () => [...queryKeys.events.all(), "list"] as const,
    list: (params?: {
      tenantId?: TenantId;
      venueId?: VenueId;
      filters?: Record<string, unknown>;
    }) => [...queryKeys.events.lists(), params] as const,
    detail: (eventId: string) => [...queryKeys.events.all(), "detail", eventId] as const,
    byCamera: (cameraId: string) => [...queryKeys.events.all(), "camera", cameraId] as const,
    byVenue: (venueId: VenueId) => [...queryKeys.events.all(), "venue", venueId] as const,
  },

  // Alerts
  alerts: {
    all: () => ["alerts"] as const,
    lists: () => [...queryKeys.alerts.all(), "list"] as const,
    list: (params?: {
      tenantId?: TenantId;
      venueId?: VenueId;
      filters?: Record<string, unknown>;
    }) => [...queryKeys.alerts.lists(), params] as const,
    detail: (alertId: string) => [...queryKeys.alerts.all(), "detail", alertId] as const,
    active: (params?: { tenantId?: TenantId; venueId?: VenueId }) =>
      [...queryKeys.alerts.all(), "active", params] as const,
    byVenue: (venueId: VenueId) => [...queryKeys.alerts.all(), "venue", venueId] as const,
  },

  // Evidence
  evidence: {
    all: () => ["evidence"] as const,
    lists: () => [...queryKeys.evidence.all(), "list"] as const,
    list: (params?: {
      tenantId?: TenantId;
      venueId?: VenueId;
      filters?: Record<string, unknown>;
    }) => [...queryKeys.evidence.lists(), params] as const,
    detail: (evidenceId: string) => [...queryKeys.evidence.all(), "detail", evidenceId] as const,
    byEvent: (eventId: string) => [...queryKeys.evidence.all(), "event", eventId] as const,
    byVenue: (venueId: VenueId) => [...queryKeys.evidence.all(), "venue", venueId] as const,
  },

  // Analytics
  analytics: {
    all: () => ["analytics"] as const,
    occupancy: (params?: { tenantId?: TenantId; venueId?: VenueId; timeRange?: string }) =>
      [...queryKeys.analytics.all(), "occupancy", params] as const,
    trends: (params?: { tenantId?: TenantId; venueId?: VenueId; timeRange?: string }) =>
      [...queryKeys.analytics.all(), "trends", params] as const,
    heatmaps: (params?: { tenantId?: TenantId; venueId?: VenueId; timeRange?: string }) =>
      [...queryKeys.analytics.all(), "heatmaps", params] as const,
  },

  // Reports
  reports: {
    all: () => ["reports"] as const,
    lists: () => [...queryKeys.reports.all(), "list"] as const,
    list: (filters?: Record<string, unknown>) => [...queryKeys.reports.lists(), filters] as const,
    detail: (reportId: string) => [...queryKeys.reports.all(), "detail", reportId] as const,
    byVenue: (venueId: VenueId) => [...queryKeys.reports.all(), "venue", venueId] as const,
  },

  // Configuration
  config: {
    all: () => ["config"] as const,
    system: () => [...queryKeys.config.all(), "system"] as const,
    tenant: (tenantId: TenantId) => [...queryKeys.config.all(), "tenant", tenantId] as const,
    venue: (venueId: VenueId) => [...queryKeys.config.all(), "venue", venueId] as const,
  },

  // Operational vertical slice (Task 18.13)
  operational: {
    all: () => ["operational"] as const,
    event: (eventId: string) => [...queryKeys.operational.all(), "event", eventId] as const,
    fact: (factId: string) => [...queryKeys.operational.all(), "fact", factId] as const,
    eventEvidence: (eventId: string) =>
      [...queryKeys.operational.all(), "evidence", eventId] as const,
  },
} as const;

/**
 * Type helper to extract query key arrays.
 * Usage: type HealthKey = QueryKey<typeof queryKeys.health.health>;
 */
export type QueryKey<T> = T extends () => infer K ? K : never;

/**
 * Utility to invalidate all queries under a domain.
 * Usage: invalidateQueries({ queryKey: queryKeys.cameras.all() })
 */
export function invalidateDomain(
  queryClient: import("@tanstack/react-query").QueryClient,
  domain: keyof typeof queryKeys,
) {
  void queryClient.invalidateQueries({ queryKey: queryKeys[domain].all() });
}

/**
 * Utility to get the base query key for a domain.
 * Usage: getBaseKey(queryKeys.cameras) // ["cameras"]
 */
export function getBaseKey(domain: keyof typeof queryKeys) {
  return queryKeys[domain].all();
}
