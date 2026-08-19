/**
 * Test Suite: Server-State Cache Foundation
 *
 * Validates Subtask 40.6:
 * - QueryClient configuration and conservative defaults
 * - Deterministic query key factories and scoping
 * - Cache invalidation on domain events
 * - Cache clearing on logout (user data isolation)
 */

import { describe, it, expect } from "vitest";
import { createQueryClient, staleTimeConfig } from "@/query/client";
import { queryKeys, getBaseKey, invalidateDomain } from "@/query/keys";
import type { TenantId, VenueId } from "@/api/types";

describe("Server-State Cache Foundation", () => {
  it("should configure query client with conservative desktop defaults", () => {
    const client = createQueryClient();
    const defaults = client.getDefaultOptions();

    expect(defaults.queries?.staleTime).toBe(staleTimeConfig.moderate);
    expect(defaults.queries?.gcTime).toBe(300_000);
    expect(defaults.queries?.refetchOnWindowFocus).toBe(false);
    expect(defaults.queries?.refetchOnReconnect).toBe(true);
  });

  it("should generate deterministic, hierarchical query keys", () => {
    const tenantId = "tenant-123" as TenantId;
    const venueId = "venue-456" as VenueId;

    expect(queryKeys.health.health()).toEqual(["health", "health"]);
    expect(queryKeys.venues.byTenant(tenantId)).toEqual(["venues", "tenant", "tenant-123"]);
    expect(queryKeys.cameras.byVenue(venueId)).toEqual(["cameras", "venue", "venue-456"]);
    expect(queryKeys.alerts.active({ tenantId, venueId })).toEqual([
      "alerts",
      "active",
      { tenantId: "tenant-123", venueId: "venue-456" },
    ]);
  });

  it("should extract base domain keys correctly", () => {
    expect(getBaseKey("cameras")).toEqual(["cameras"]);
    expect(getBaseKey("alerts")).toEqual(["alerts"]);
    expect(getBaseKey("events")).toEqual(["events"]);
    expect(getBaseKey("health")).toEqual(["health"]);
  });

  it("should completely clear query cache on logout", () => {
    const client = createQueryClient();

    // Populate cache with User A's data
    client.setQueryData(["auth", "current-user"], { user_id: "user-a", display_name: "User A" });
    client.setQueryData(["alerts", "list"], [{ id: "alert-1", title: "Door Open" }]);
    client.setQueryData(["cameras", "list"], [{ id: "cam-1", name: "Lobby" }]);

    expect(client.getQueryData(["auth", "current-user"])).toBeDefined();
    expect(client.getQueryData(["alerts", "list"])).toBeDefined();
    expect(client.getQueryData(["cameras", "list"])).toBeDefined();

    // Simulate logout purge
    client.clear();

    expect(client.getQueryData(["auth", "current-user"])).toBeUndefined();
    expect(client.getQueryData(["alerts", "list"])).toBeUndefined();
    expect(client.getQueryData(["cameras", "list"])).toBeUndefined();
  });

  it("should target specific domain invalidation", () => {
    const client = createQueryClient();

    client.setQueryData(["cameras", "list"], [{ id: "cam-1" }]);
    client.setQueryData(["alerts", "list"], [{ id: "alert-1" }]);

    invalidateDomain(client, "cameras");

    const cameraQuery = client.getQueryCache().find({ queryKey: ["cameras", "list"] });
    const alertQuery = client.getQueryCache().find({ queryKey: ["alerts", "list"] });

    expect(cameraQuery?.isStale()).toBe(true);
    expect(alertQuery?.isStale()).toBe(false);
  });
});
