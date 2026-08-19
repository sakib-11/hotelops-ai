/**
 * Health Service - API service for health and readiness endpoints.
 *
 * Provides typed access to the FastAPI health check endpoints.
 */

import { httpClient } from "../client";
import type { HealthResponse, ReadinessResponse } from "../types";

/**
 * Health check - liveness probe.
 * Returns 200 if the application process is alive.
 * Does NOT check external dependencies.
 */
export async function getHealth(): Promise<HealthResponse> {
  const response = await httpClient.get<HealthResponse>("/health");
  return response.data;
}

/**
 * Readiness check - checks all mandatory dependencies.
 * Returns 200 with status=ready when all dependencies are healthy.
 * Returns 503 with status=not_ready when any dependency is unavailable.
 */
export async function getReadiness(): Promise<ReadinessResponse> {
  const response = await httpClient.get<ReadinessResponse>("/ready");
  return response.data;
}

/**
 * Check if the API is healthy and ready.
 * Returns true if both health and readiness are ok.
 */
export async function isApiReady(): Promise<boolean> {
  try {
    const [health, readiness] = await Promise.all([getHealth(), getReadiness()]);
    return health.status === "ok" && readiness.status === "ready";
  } catch {
    return false;
  }
}

/**
 * Get API version info from health endpoint.
 */
export async function getApiVersion(): Promise<{ service: string; version: string } | null> {
  try {
    const response = await httpClient.get<HealthResponse>("/health");
    return {
      service: response.data.service,
      version: response.data.version,
    };
  } catch {
    return null;
  }
}

export { getHealth as healthCheck, getReadiness as readinessCheck };
export type { HealthResponse, ReadinessResponse };
