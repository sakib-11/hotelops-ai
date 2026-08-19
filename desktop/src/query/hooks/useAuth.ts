/**
 * Auth Query Hooks - Server-state cache for authentication state.
 *
 * These hooks integrate with Task 40.4 authentication store
 * and Task 40.5 API client for server-state management.
 *
 * Note: Authentication state (session, user) is primarily managed
 * by the zustand store (Task 40.4). These hooks provide server-state
 * synchronization for the current user profile.
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
import { getCurrentUser, verifyToken, type UserInfo } from "@/api/services/auth";
import { useAuthStore } from "@/features/auth/hooks/useAuthStore";

/**
 * Hook for current user profile.
 *
 * Fetches user profile from API and syncs with auth store.
 * This is the server-state representation of the authenticated user.
 *
 * Stale time: moderate (2 min) - user profile doesn't change often.
 *
 * Note: This query is only enabled when user is authenticated.
 * The actual auth state is managed by the zustand store (Task 40.4).
 */
export function useCurrentUser(
  options?: Partial<
    UseQueryOptions<UserInfo, Error, UserInfo, ReturnType<typeof queryKeys.auth.currentUser>>
  >,
): UseQueryResult<UserInfo> {
  const isAuthenticated = useAuthStore((state) => state.status === "authenticated");

  return useQuery({
    queryKey: queryKeys.auth.currentUser(),
    queryFn: getCurrentUser,
    staleTime: 120_000, // 2 minutes - user profile doesn't change often
    enabled: isAuthenticated, // Only fetch when authenticated
    retry: (failureCount, error) => {
      // Don't retry on 401 - let auth layer handle it
      if (error instanceof Error && "status" in error) {
        const status = (error as { status: number }).status;
        if (status === 401) return false;
      }
      return failureCount < 3;
    },
    ...options,
  });
}

/**
 * Hook for token verification.
 *
 * Lightweight check to verify token validity without full user fetch.
 * Used for session validation and background checks.
 *
 * Stale time: fast (30s) - token validity can change.
 */
export function useTokenVerification(
  options?: Partial<
    UseQueryOptions<boolean, Error, boolean, ReturnType<typeof queryKeys.auth.session>>
  >,
): UseQueryResult<boolean> {
  const hasToken = useAuthStore((state) => !!state.tokens?.access_token);

  return useQuery({
    queryKey: queryKeys.auth.session(),
    queryFn: verifyToken,
    staleTime: 30_000, // 30 seconds
    enabled: hasToken,
    retry: false, // Don't retry verification failures
    ...options,
  });
}

/**
 * Hook for user profile with automatic sync to auth store.
 *
 * This hook fetches user profile and automatically syncs it
 * to the auth store. Use when you need guaranteed sync.
 */
export function useCurrentUserWithSync(
  options?: Partial<
    UseQueryOptions<UserInfo, Error, UserInfo, ReturnType<typeof queryKeys.auth.currentUser>>
  >,
): UseQueryResult<UserInfo> {
  const { setUser } = useAuthStore();

  return useQuery({
    queryKey: queryKeys.auth.currentUser(),
    queryFn: async () => {
      const user = await getCurrentUser();
      setUser(user); // Sync to auth store
      return user;
    },
    staleTime: 120_000,
    enabled: useAuthStore.getState().status === "authenticated",
    ...options,
  });
}

/**
 * Mutation for refreshing user profile.
 *
 * Useful after profile updates or when manual refresh is needed.
 */
export function useRefreshUser(
  options?: Partial<UseMutationOptions<UserInfo>>,
): UseMutationResult<UserInfo, Error, void> {
  const { setUser } = useAuthStore();

  return useMutation({
    mutationFn: async () => {
      const user = await getCurrentUser();
      setUser(user);
      return user;
    },
    onSuccess: () => {
      // Invalidate related queries
      queryClient.invalidateQueries({ queryKey: queryKeys.auth.currentUser() });
    },
    ...options,
  });
}

/**
 * Prefetch current user profile.
 * Useful for anticipating user navigation.
 */
export async function prefetchCurrentUser(): Promise<void> {
  const isAuthenticated = useAuthStore.getState().status === "authenticated";
  if (!isAuthenticated) return;

  await queryClient.prefetchQuery({
    queryKey: queryKeys.auth.currentUser(),
    queryFn: getCurrentUser,
    staleTime: 120_000,
  });
}

/**
 * Invalidate auth queries (user profile, session).
 * Use after profile updates or auth changes.
 */
export function invalidateAuthQueries(): void {
  queryClient.invalidateQueries({ queryKey: queryKeys.auth.all() });
}

/**
 * Set user data directly in cache (e.g., after mutation or WebSocket update).
 */
export function setCurrentUserData(user: import("@/api/types").UserInfo): void {
  queryClient.setQueryData(queryKeys.auth.currentUser(), user);
}

/**
 * Clear auth queries from cache.
 * Use on logout to clear user-specific data.
 */
export function clearAuthQueries(): void {
  queryClient.removeQueries({ queryKey: queryKeys.auth.all() });
}
