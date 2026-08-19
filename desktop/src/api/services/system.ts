/**
 * System Service - Generic API utilities and system-level operations.
 *
 * Provides common API functionality used across the application.
 */

import { httpClient } from "../client";
import type { HealthResponse, ApiResponse } from "../types";
import { getApiVersion } from "./health";

export type { ApiResponse };

/**
 * Ping the API to check connectivity.
 * Useful for connection testing before critical operations.
 */
export async function ping(): Promise<boolean> {
  try {
    const response: ApiResponse<HealthResponse> = await httpClient.get<HealthResponse>("/health");
    return response.data.status === "ok";
  } catch {
    return false;
  }
}

export { getApiVersion };

/**
 * Make a generic GET request with full response metadata.
 * Useful for endpoints not yet covered by specific services.
 */
export async function get<T>(url: string, params?: Record<string, unknown>) {
  return httpClient.get<T>(url, params);
}

/**
 * Make a generic POST request with full response metadata.
 */
export async function post<T>(url: string, data?: unknown) {
  return httpClient.post<T>(url, data);
}

/**
 * Make a generic PUT request.
 */
export async function put<T>(url: string, data?: unknown) {
  return httpClient.put<T>(url, data);
}

/**
 * Make a generic PATCH request.
 */
export async function patch<T>(url: string, data?: unknown) {
  return httpClient.patch<T>(url, data);
}

/**
 * Make a generic DELETE request.
 */
export async function del<T>(url: string) {
  return httpClient.delete<T>(url);
}

/**
 * Make a request with custom configuration.
 * Use sparingly - prefer specific service methods.
 */
export async function request<T>(config: {
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  url: string;
  params?: Record<string, unknown>;
  data?: unknown;
  headers?: Record<string, string>;
  timeout?: number;
  signal?: AbortSignal;
}) {
  return httpClient.request<T>(config);
}
