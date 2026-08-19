import type { LoginCredentials, AuthResponse, UserInfo, AuthTokens, AuthError } from "../types";

/**
 * AuthService Interface - Frontend Contract
 *
 * This interface defines the authentication operations the frontend needs.
 * The implementation will call the FastAPI backend.
 *
 * IMPORTANT: Several backend endpoints are MISSING and need to be implemented:
 * - POST /auth/login
 * - POST /auth/refresh  
 * - POST /auth/logout
 * - GET /auth/me
 *
 * Until backend implements these, we use a stub adapter.
 */

export interface AuthService {
  /**
   * Authenticate user with email/password
   * Expected backend: POST /auth/login
   * Request: { email, password }
   * Response: { access_token, token_type, expires_in, user }
   */
  login(credentials: LoginCredentials): Promise<AuthResponse>;

  /**
   * Register new user (if backend supports)
   * Expected backend: POST /auth/register
   */
  register?(data: { email: string; password: string; display_name: string }): Promise<AuthResponse>;

  /**
   * Refresh access token
   * Expected backend: POST /auth/refresh
   */
  refreshToken?(refreshToken: string): Promise<AuthTokens>;

  /**
   * Logout - invalidate server session
   * Expected backend: POST /auth/logout
   */
  logout(): Promise<void>;

  /**
   * Get current user info / validate session
   * Expected backend: GET /auth/me
   */
  getCurrentUser(): Promise<UserInfo>;

  /**
   * Verify token validity without full user fetch
   */
  verifyToken?(): Promise<boolean>;

  /**
   * Clear stored tokens (for cleanup on logout/session expiry)
   */
  clearTokens?(): void;
}

export class AuthServiceError extends Error {
  public readonly code: AuthError["code"];
  public readonly status?: number;

  constructor(error: AuthError) {
    super(error.message);
    this.name = "AuthServiceError";
    this.code = error.code;
    this.status = error.status;
  }
}

/**
 * Create standardized auth error from various failure types
 */
export function createAuthError(error: unknown, defaultMessage: string): AuthServiceError {
  if (error instanceof Response) {
    return new AuthServiceError({
      code: mapHttpStatusToAuthCode(error.status),
      message: defaultMessage,
      status: error.status,
    });
  }

  if (error instanceof TypeError && error.message.includes("fetch")) {
    return new AuthServiceError({
      code: "network_error",
      message: "Unable to connect to the HotelOps AI service.",
    });
  }

  if (error instanceof AuthServiceError) {
    return error;
  }

  return new AuthServiceError({
    code: "unknown",
    message: defaultMessage,
  });
}

function mapHttpStatusToAuthCode(status: number): AuthError["code"] {
  switch (status) {
    case 401:
      return "invalid_credentials";
    case 403:
      return "permission_denied";
    case 404:
      return "server_error";
    case 500:
    case 502:
    case 503:
      return "server_error";
    default:
      return "unknown";
  }
}

/**
 * Configuration for auth service
 */
export interface AuthServiceConfig {
  baseUrl: string;
  // Timeout for auth requests
  timeout?: number;
  // Whether to use credentials (cookies) - for future refresh token support
  credentials?: "include" | "omit" | "same-origin";
}

/**
 * Default auth service configuration
 * In production, this should come from environment config
 */
const apiBaseUrl =
  (import.meta.env as Record<string, string | undefined>).VITE_API_BASE_URL ??
  "http://localhost:8000";

export const defaultAuthConfig: AuthServiceConfig = {
  baseUrl: apiBaseUrl,
  timeout: 10000,
  credentials: "omit", // Bearer tokens in Authorization header
};
