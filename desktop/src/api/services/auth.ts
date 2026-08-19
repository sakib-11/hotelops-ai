/**
 * Auth Service - API service for authentication endpoints.
 *
 * Integrates with Task 40.4 auth store for token management.
 * All authentication HTTP requests go through this service.
 */

import { httpClient } from "../client";
import type { LoginCredentials, AuthResponse, UserInfo, AuthTokens } from "../types";
import { useAuthStore } from "@/features/auth/hooks/useAuthStore";

/**
 * Login with email and password.
 * Stores tokens in auth store on success.
 */
export async function login(credentials: LoginCredentials): Promise<AuthResponse> {
  const response = await httpClient.post<AuthResponse>("/auth/login", credentials);

  // Store tokens in auth store
  const authStore = useAuthStore.getState();
  authStore.setTokens(response.data.tokens);
  authStore.setUser(response.data.user);

  return response.data;
}

/**
 * Register a new user (if backend supports).
 */
export async function register(data: {
  email: string;
  password: string;
  display_name: string;
}): Promise<AuthResponse> {
  const response = await httpClient.post<AuthResponse>("/auth/register", data);
  return response.data;
}

/**
 * Refresh the access token using refresh token.
 * Updates tokens in auth store on success.
 */
export async function refreshToken(): Promise<AuthTokens> {
  const authStore = useAuthStore.getState();
  const refreshToken = authStore.tokens?.refresh_token;

  if (!refreshToken) {
    throw new Error("No refresh token available");
  }

  const response = await httpClient.post<AuthTokens>("/auth/refresh", {
    refresh_token: refreshToken,
  });

  // Update tokens in auth store
  authStore.setTokens(response.data);

  return response.data;
}

/**
 * Logout - invalidate server session.
 * Clears local auth state regardless of server response.
 */
export async function logout(): Promise<void> {
  const authStore = useAuthStore.getState();

  try {
    await httpClient.post("/auth/logout");
  } catch (error) {
    // Ignore server logout errors - we clear local state anyway
    console.warn("[AuthService] Server logout failed, clearing local state:", error);
  } finally {
    // Always clear local state
    authStore.setTokens(null);
    authStore.setUser(null);
  }
}

/**
 * Get current user info / validate session.
 * Returns user info if session is valid.
 */
export async function getCurrentUser(): Promise<UserInfo> {
  const response = await httpClient.get<UserInfo>("/auth/me");
  return response.data;
}

/**
 * Verify token validity without full user fetch.
 * Returns true if token is valid.
 */
export async function verifyToken(): Promise<boolean> {
  try {
    await getCurrentUser();
    return true;
  } catch {
    return false;
  }
}

/**
 * Check if error is an authentication error (401).
 * Used by interceptors to trigger session expiration flow.
 */
export function isAuthError(error: unknown): boolean {
  return (
    error instanceof Error &&
    "code" in error &&
    (error as { code: string }).code === "UNAUTHENTICATED"
  );
}

export { login as authLogin, logout as authLogout, refreshToken as authRefreshToken };
export type { UserInfo, AuthTokens, AuthResponse, LoginCredentials };
