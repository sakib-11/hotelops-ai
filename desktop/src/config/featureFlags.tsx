/**
 * Feature Flags - Fail-closed Feature Availability System
 *
 * Implements Subtask 40.9:
 * - Controls feature visibility and route availability
 * - Safe fail-closed defaults for privileged capabilities
 * - Environment and role-based evaluation
 */

import React from "react";
import { useAuthStore } from "@/features/auth/hooks/useAuthStore";
import type { RoleName } from "@/api/types";

export type FeatureFlagKey =
  | "liveMonitoring"
  | "recordings"
  | "deepAnalysis"
  | "alerts"
  | "approvals"
  | "adminSettings"
  | "aiInsights";

export interface FeatureFlagConfig {
  readonly enabled: boolean;
  readonly minRole?: RoleName;
  readonly description: string;
}

/**
 * Authoritative feature flag definitions with fail-closed defaults.
 */
export const FEATURE_FLAGS: Record<FeatureFlagKey, FeatureFlagConfig> = {
  liveMonitoring: {
    enabled: true,
    minRole: "operator",
    description: "Real-time camera streaming and live floor monitoring",
  },
  recordings: {
    enabled: true,
    minRole: "operator",
    description: "Historical recording footage retrieval and playback",
  },
  deepAnalysis: {
    enabled: true,
    minRole: "operator",
    description: "Operational trends, occupancy analytics, and heatmaps",
  },
  alerts: {
    enabled: true,
    minRole: "operator",
    description: "Real-time alerting and acknowledgment queues",
  },
  approvals: {
    enabled: true,
    minRole: "manager",
    description: "AI recommendations and supervisor approvals",
  },
  adminSettings: {
    enabled: true,
    minRole: "admin",
    description: "Tenant configuration, user permissions, and camera provisioning",
  },
  aiInsights: {
    enabled: true,
    minRole: "operator",
    description: "AI operational assistant and intelligence feed",
  },
};

const ROLE_LEVELS: Record<RoleName, number> = {
  operator: 1,
  manager: 2,
  admin: 3,
};

/**
 * Pure evaluation function for a feature flag.
 */
export function isFeatureEnabled(
  flagKey: FeatureFlagKey,
  userRole?: RoleName | null,
  envOverrides: Record<string, string | undefined> = import.meta.env,
): boolean {
  const config = FEATURE_FLAGS[flagKey];
  if (!config) {
    return false; // Fail-closed for unknown flags
  }

  // Check explicit environment variable override: VITE_FEATURE_<FLAG_NAME>=true|false
  const envVarKey = `VITE_FEATURE_${flagKey.replace(/([A-Z])/g, "_$1").toUpperCase()}`;
  const envOverride = envOverrides[envVarKey];

  if (envOverride === "false") return false;
  if (envOverride === "true") return true;

  if (!config.enabled) {
    return false;
  }

  // If feature requires a minimum role, verify user's role
  if (config.minRole) {
    if (!userRole) return false; // Fail-closed for unauthenticated/unassigned
    const userLevel = ROLE_LEVELS[userRole] ?? 0;
    const requiredLevel = ROLE_LEVELS[config.minRole] ?? 99;
    return userLevel >= requiredLevel;
  }

  return true;
}

/**
 * Hook to check if a feature flag is enabled for the current authenticated user.
 */
export function useFeatureFlag(flagKey: FeatureFlagKey): boolean {
  const user = useAuthStore((state) => state.user);
  return isFeatureEnabled(flagKey, user?.role_name);
}

/**
 * Declarative component to conditionally render children based on a feature flag.
 */
export interface FeatureGateProps {
  flag: FeatureFlagKey;
  children: React.ReactNode;
  fallback?: React.ReactNode;
}

export function FeatureGate({ flag, children, fallback = null }: FeatureGateProps) {
  const enabled = useFeatureFlag(flag);
  return enabled ? <>{children}</> : <>{fallback}</>;
}
