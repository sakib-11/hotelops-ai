/**
 * System Query Hooks - Generic query hooks for system-level operations.
 *
 * Provides hooks for ping, version, and generic CRUD operations.
 * These are low-level hooks for endpoints not yet covered by domain-specific hooks.
 */

import {
  useQuery,
  useMutation,
  type UseQueryOptions,
  type UseMutationOptions,
  type UseQueryResult,
  type UseMutationResult,
} from "@tanstack/react-query";
import { queryClient } from "../client";
import { queryKeys } from "../keys";
import {
  ping,
  getApiVersion,
  get,
  post,
  put,
  patch,
  del,
  type ApiResponse,
} from "@/api/services/system";

/**
 * Hook for API connectivity check.
 *
 * Returns true if the API is reachable and healthy.
 * Stale time: fast (30s) - connectivity can change.
 */
export function usePing(
  options?: Partial<
    UseQueryOptions<boolean, Error, boolean, ReturnType<typeof queryKeys.health.ping>>
  >,
): UseQueryResult<boolean> {
  return useQuery({
    queryKey: queryKeys.health.ping(),
    queryFn: ping,
    staleTime: 30_000, // 30 seconds
    retry: 2,
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
    queryFn: getApiVersion,
    staleTime: 600_000, // 10 minutes
    retry: 1,
    ...options,
  });
}

/**
 * Generic GET query hook.
 * Use for endpoints not yet covered by domain-specific hooks.
 */
export function useApiGet<T>(
  url: string,
  params?: Record<string, unknown>,
  options?: Partial<UseQueryOptions<T, Error, T>>,
): UseQueryResult<T> {
  return useQuery({
    queryKey: ["api", "get", url, params] as const,
    queryFn: () => get<T>(url, params).then((r) => r.data),
    ...options,
  });
}

/**
 * Generic POST mutation hook.
 */
export function useApiPost<T, TVariables>(
  url: string,
  options?: Partial<UseMutationOptions<ApiResponse<T>, Error, TVariables>>,
): UseMutationResult<ApiResponse<T>, Error, TVariables> {
  return useMutation({
    mutationFn: (variables: TVariables) => post<T>(url, variables),
    ...options,
  });
}

/**
 * Generic PUT mutation hook.
 */
export function useApiPut<T, TVariables>(
  url: string,
  options?: Partial<UseMutationOptions<ApiResponse<T>, Error, TVariables>>,
): UseMutationResult<ApiResponse<T>, Error, TVariables> {
  return useMutation({
    mutationFn: (variables: TVariables) => put<T>(url, variables),
    ...options,
  });
}

/**
 * Generic PATCH mutation hook.
 */
export function useApiPatch<T, TVariables>(
  url: string,
  options?: Partial<UseMutationOptions<ApiResponse<T>, Error, TVariables>>,
): UseMutationResult<ApiResponse<T>, Error, TVariables> {
  return useMutation({
    mutationFn: (variables: TVariables) => patch<T>(url, variables),
    ...options,
  });
}

/**
 * Generic DELETE mutation hook.
 */
export function useApiDelete(
  url: string,
  options?: Partial<UseMutationOptions<ApiResponse<undefined>, Error, undefined>>,
): UseMutationResult<ApiResponse<undefined>, Error, undefined> {
  return useMutation({
    mutationFn: () => del<undefined>(url),
    ...options,
  });
}

/**
 * Generic request mutation hook with full config.
 */
export function useApiRequest<T>(
  options?: Partial<
    UseMutationOptions<
      ApiResponse<T>,
      Error,
      {
        method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
        url: string;
        params?: Record<string, unknown>;
        data?: unknown;
        headers?: Record<string, string>;
        timeout?: number;
        signal?: AbortSignal;
      }
    >
  >,
): UseMutationResult<
  ApiResponse<T>,
  Error,
  {
    method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
    url: string;
    params?: Record<string, unknown>;
    data?: unknown;
    headers?: Record<string, string>;
    timeout?: number;
    signal?: AbortSignal;
  }
> {
  return useMutation({
    mutationFn: (config) => import("@/api/services/system").then((m) => m.request<T>(config)),
    ...options,
  });
}

/**
 * Prefetch a generic GET endpoint.
 */
export async function prefetchApiGet<T>(
  url: string,
  params?: Record<string, unknown>,
): Promise<void> {
  await queryClient.prefetchQuery({
    queryKey: ["api", "get", url, params] as const,
    queryFn: () => get<T>(url, params).then((r) => r.data),
  });
}
