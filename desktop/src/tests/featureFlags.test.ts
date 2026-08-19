/**
 * Test Suite: Feature Flag System
 *
 * Validates Subtask 40.9:
 * - Fail-closed defaults for unknown flags
 * - Role-based authorization levels (operator, manager, admin)
 * - Environment variable overrides
 */

import { describe, it, expect } from "vitest";
import { isFeatureEnabled } from "@/config/featureFlags";

describe("Feature Flag System", () => {
  it("should fail-closed for unknown feature flags", () => {
    // @ts-expect-error Testing invalid key runtime behavior
    expect(isFeatureEnabled("unknownNonExistentFlag", "admin")).toBe(false);
  });

  it("should allow operator role for operator-level features", () => {
    expect(isFeatureEnabled("liveMonitoring", "operator")).toBe(true);
    expect(isFeatureEnabled("alerts", "operator")).toBe(true);
  });

  it("should restrict manager-level features from operator role", () => {
    expect(isFeatureEnabled("approvals", "operator")).toBe(false);
    expect(isFeatureEnabled("approvals", "manager")).toBe(true);
    expect(isFeatureEnabled("approvals", "admin")).toBe(true);
  });

  it("should restrict admin-level features from operator and manager roles", () => {
    expect(isFeatureEnabled("adminSettings", "operator")).toBe(false);
    expect(isFeatureEnabled("adminSettings", "manager")).toBe(false);
    expect(isFeatureEnabled("adminSettings", "admin")).toBe(true);
  });

  it("should respect environment variable overrides", () => {
    // Force disabled via env override
    const disabledOverride = { VITE_FEATURE_LIVE_MONITORING: "false" };
    expect(isFeatureEnabled("liveMonitoring", "admin", disabledOverride)).toBe(false);

    // Force enabled via env override
    const enabledOverride = { VITE_FEATURE_APPROVALS: "true" };
    expect(isFeatureEnabled("approvals", "operator", enabledOverride)).toBe(true);
  });
});
