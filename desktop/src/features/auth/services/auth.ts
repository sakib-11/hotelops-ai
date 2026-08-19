/**
 * Auth Service - API service for authentication endpoints.
 *
 * Integrates with Task 40.4 auth store for token management.
 * Uses the centralized HTTP client from the API client layer.
 *
 * NOTE: Several endpoints are NOT YET IMPLEMENTED in backend:
 * - POST /auth/login
 * - POST /auth/refresh
 * - POST /auth/logout
 * - GET /auth/me
 *
 * This service will fail gracefully until backend implements them.
 */

import { httpClient } from "@/api/client";
import type { AuthTokens, UserInfo } from "@/features/auth/types";

/**
 * Login with email and password.
 * Returns tokens and user info on success.
 */
export async function login(credentials: { email: string; password: string }): Promise<{
  tokens: AuthTokens;
  user: UserInfo;
}> {
  const response = await httpClient.post<{
    tokens: AuthTokens;
    user: UserInfo;
  }>("/auth/login", credentials);

  return response.data;
}

/**
 * Register a new user (if backend supports).
 */
export async function register(data: {
  email: string;
  password: string;
  display_name: string;
}): Promise<{
  tokens: AuthTokens;
  user: UserInfo;
}> {
  const response = await httpClient.post<{
    tokens: AuthTokens;
    user: UserInfo;
  }>("/auth/register", data);

  return response.data;
}

/**
 * Refresh the access token using refresh token.
 */
export async function refreshToken(refreshToken: string): Promise<AuthTokens> {
  const response = await httpClient.post<AuthTokens>("/auth/refresh", {
    refresh_token: refreshToken,
  });

  return response.data;
}

/**
 * Logout - invalidate server session.
 */
export async function logout(): Promise<void> {
  try {
    await httpClient.post("/auth/logout");
  } catch (error) {
    // Ignore server logout errors - we clear local state anyway
    console.warn("[AuthService] Server logout failed, clearing local state:", error);
  }
}

/**
 * Get current user info / validate session.
 */
export async function getCurrentUser(): Promise<UserInfo> {
  const response = await httpClient.get<UserInfo>("/auth/me");
  return response.data;
}

/**
 * Verify token validity without full user fetch.
 */
export async function verifyToken(): Promise<boolean> {
  try {
    await getCurrentUser();
    return true;
  } catch {
    return false;
  }
}

export { login as authLogin, logout as authLogout, refreshToken as authRefreshToken };
