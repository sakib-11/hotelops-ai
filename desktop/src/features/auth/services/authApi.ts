import {
  AuthServiceConfig,
  AuthServiceError,
  createAuthError,
  defaultAuthConfig,
} from "./authService";

/**
 * Production AuthService Implementation
 * Calls the FastAPI backend endpoints
 *
 * NOTE: Several endpoints are NOT YET IMPLEMENTED in backend:
 * - POST /auth/login
 * - POST /auth/refresh
 * - POST /auth/logout
 * - GET /auth/me
 *
 * This implementation will fail gracefully until backend implements them.
 */

function getAuthHeaders(accessToken?: string): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (accessToken) {
    headers.Authorization = `Bearer ${accessToken}`;
  }
  return headers;
}

interface ErrorResponse {
  detail?: string;
}

async function handleResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let errorMessage = "Request failed";
    try {
      const errorData = (await response.json()) as ErrorResponse;
      errorMessage = errorData.detail ?? errorMessage;
    } catch {
      // Use status text if no JSON body
      const statusText = response.statusText;
      errorMessage = statusText ? statusText : `HTTP ${String(response.status)}`;
    }
    throw new AuthServiceError({
      code: mapStatusToCode(response.status),
      message: errorMessage,
      status: response.status,
    });
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return response.json() as Promise<T>;
}

function mapStatusToCode(
  status: number,
):
  | "invalid_credentials"
  | "network_error"
  | "server_error"
  | "session_expired"
  | "permission_denied"
  | "unknown" {
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

function createAuthFetch(config: AuthServiceConfig) {
  const { baseUrl, timeout = 10000, credentials = "omit" } = config;

  return async function authFetch<T>(
    endpoint: string,
    options: RequestInit = {},
    accessToken?: string,
  ): Promise<T> {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => {
      controller.abort();
    }, timeout);

    try {
      const response = await fetch(`${baseUrl}${endpoint}`, {
        ...options,
        headers: (() => {
          const baseHeaders = getAuthHeaders(accessToken);
          const optionsHeaders = options.headers;
          let mergedHeaders: Record<string, string> = { ...baseHeaders };

          if (optionsHeaders) {
            if (optionsHeaders instanceof Headers) {
              optionsHeaders.forEach((value, key) => {
                mergedHeaders[key] = value;
              });
            } else if (Array.isArray(optionsHeaders)) {
              for (const [key, value] of optionsHeaders) {
                mergedHeaders[key] = value;
              }
            } else {
              mergedHeaders = { ...mergedHeaders, ...optionsHeaders };
            }
          }

          return mergedHeaders;
        })(),
        credentials,
        signal: controller.signal,
      });

      clearTimeout(timeoutId);
      return await handleResponse<T>(response);
    } catch (error) {
      clearTimeout(timeoutId);
      if (error instanceof AuthServiceError) throw error;
      if (error instanceof DOMException && error.name === "AbortError") {
        throw new AuthServiceError({
          code: "network_error",
          message: "Request timed out. Please try again.",
        });
      }
      throw createAuthError(error, "An unexpected error occurred");
    }
  };
}

export function createAuthService(config: AuthServiceConfig = defaultAuthConfig) {
  const authFetch = createAuthFetch(config);

  // Store tokens in memory (will be managed by auth store)
  let currentAccessToken: string | null = null;
  let currentRefreshToken: string | null = null;

  const setTokens = (tokens: { access_token: string; refresh_token?: string }) => {
    currentAccessToken = tokens.access_token;
    currentRefreshToken = tokens.refresh_token ?? null;
  };

  const clearTokens = () => {
    currentAccessToken = null;
    currentRefreshToken = null;
  };

  const getAccessToken = () => currentAccessToken;

  return {
    // Set/get tokens (called by auth store)
    setTokens,
    clearTokens,
    getAccessToken,

    // Login with email/password
    async login(credentials: { email: string; password: string }): Promise<{
      tokens: {
        access_token: string;
        token_type: string;
        expires_in: number;
        refresh_token?: string;
      };
      user: {
        user_id: string;
        email: string;
        display_name: string;
        tenant_id: string;
        role_name: string;
        permissions: string[];
        venue_scope: string[];
        status: string;
      };
    }> {
      const response = await authFetch<{
        access_token: string;
        token_type: string;
        expires_in: number;
        refresh_token?: string;
        user: {
          user_id: string;
          email: string;
          display_name: string;
          tenant_id: string;
          role_name: string;
          permissions: string[];
          venue_scope: string[];
          status: string;
        };
      }>("/auth/login", {
        method: "POST",
        body: JSON.stringify(credentials),
      });

      setTokens({
        access_token: response.access_token,
        refresh_token: response.refresh_token,
      });

      return {
        tokens: {
          access_token: response.access_token,
          token_type: response.token_type,
          expires_in: response.expires_in,
          refresh_token: response.refresh_token,
        },
        user: response.user,
      };
    },

    // Refresh access token
    async refreshToken(): Promise<{
      access_token: string;
      token_type: string;
      expires_in: number;
      refresh_token?: string;
    }> {
      if (!currentRefreshToken) {
        throw new AuthServiceError({
          code: "session_expired",
          message: "No refresh token available",
        });
      }

      const response = await authFetch<{
        access_token: string;
        token_type: string;
        expires_in: number;
        refresh_token?: string;
      }>("/auth/refresh", {
        method: "POST",
        body: JSON.stringify({ refresh_token: currentRefreshToken }),
      });

      setTokens({
        access_token: response.access_token,
        refresh_token: response.refresh_token ?? currentRefreshToken,
      });

      return response;
    },

    // Logout - invalidate server session
    async logout(): Promise<void> {
      if (currentAccessToken) {
        try {
          await authFetch(
            "/auth/logout",
            {
              method: "POST",
            },
            currentAccessToken,
          );
        } catch {
          // Ignore logout errors - we clear local state anyway
        }
      }
      clearTokens();
    },

    // Get current user info (validates session)
    async getCurrentUser(): Promise<{
      user_id: string;
      email: string;
      display_name: string;
      tenant_id: string;
      role_name: string;
      permissions: string[];
      venue_scope: string[];
      status: string;
    }> {
      if (!currentAccessToken) {
        throw new AuthServiceError({
          code: "session_expired",
          message: "No active session",
        });
      }

      return authFetch("/auth/me", {}, currentAccessToken);
    },

    // Verify token is still valid
    async verifyToken(): Promise<boolean> {
      if (!currentAccessToken) return false;
      try {
        await this.getCurrentUser();
        return true;
      } catch {
        return false;
      }
    },
  };
}

export type AuthServiceInstance = ReturnType<typeof createAuthService>;
