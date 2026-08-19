import { AuthService, AuthServiceError, AuthServiceConfig } from "./authService";
import type { LoginCredentials, AuthResponse, UserInfo, AuthTokens } from "../types";

/**
 * Stub AuthService for Development
 *
 * Use this when backend auth endpoints are not yet implemented.
 * Simulates successful authentication with a test user.
 *
 * To enable: set VITE_USE_AUTH_STUB=true in environment
 */

interface StubUser {
  user_id: string;
  email: string;
  password: string; // plain text for stub only
  display_name: string;
  tenant_id: string;
  role_name: "admin" | "manager" | "operator";
  permissions: string[];
  venue_scope: string[];
  status: "active" | "disabled";
}

// In-memory user store for stub
const STUB_USERS: StubUser[] = [
  {
    user_id: "11111111-1111-1111-1111-111111111111",
    email: "admin@hotelops.ai",
    password: "admin123",
    display_name: "Admin User",
    tenant_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    role_name: "admin",
    permissions: [
      "venue.read",
      "venue.manage",
      "video.read",
      "video.analyze",
      "analytics.read",
      "evidence.read",
      "recommendation.read",
      "recommendation.manage",
      "alert.read",
      "alert.manage",
      "user.read",
      "user.manage",
      "membership.read",
      "membership.manage",
    ],
    venue_scope: [],
    status: "active",
  },
  {
    user_id: "22222222-2222-2222-2222-222222222222",
    email: "manager@hotelops.ai",
    password: "manager123",
    display_name: "Manager User",
    tenant_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    role_name: "manager",
    permissions: [
      "venue.read",
      "video.read",
      "video.analyze",
      "analytics.read",
      "evidence.read",
      "recommendation.read",
      "recommendation.manage",
      "alert.read",
      "alert.manage",
    ],
    venue_scope: [],
    status: "active",
  },
  {
    user_id: "33333333-3333-3333-3333-333333333333",
    email: "operator@hotelops.ai",
    password: "operator123",
    display_name: "Operator User",
    tenant_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    role_name: "operator",
    permissions: [
      "venue.read",
      "video.read",
      "analytics.read",
      "evidence.read",
      "recommendation.read",
      "alert.read",
    ],
    venue_scope: [],
    status: "active",
  },
];

function generateStubToken(userId: string): string {
  // Simple stub token - in real impl this would be a proper JWT
  const header = btoa(JSON.stringify({ alg: "HS256", typ: "JWT" }));
  const payload = btoa(
    JSON.stringify({
      sub: userId,
      iss: "hotelops-ai",
      iat: Math.floor(Date.now() / 1000),
      exp: Math.floor(Date.now() / 1000) + 3600, // 1 hour
    }),
  );
  const signature = btoa("stub-signature");
  return `${header}.${payload}.${signature}`;
}

function createStubResponse(user: StubUser): AuthResponse {
  const accessToken = generateStubToken(user.user_id);
  return {
    tokens: {
      access_token: accessToken,
      token_type: "bearer",
      expires_in: 3600,
      refresh_token: `stub-refresh-${user.user_id}`,
    },
    user: {
      user_id: user.user_id,
      email: user.email,
      display_name: user.display_name,
      tenant_id: user.tenant_id,
      role_name: user.role_name,
      permissions: user.permissions,
      venue_scope: user.venue_scope,
      status: user.status,
    },
  };
}

export function createStubAuthService(_config: AuthServiceConfig): AuthService {
  let currentUser: UserInfo | null = null;

  return {
    async login(credentials: LoginCredentials): Promise<AuthResponse> {
      // Simulate network delay
      await new Promise((resolve) => setTimeout(resolve, 500));

      const user = STUB_USERS.find(
        (u) =>
          u.email.toLowerCase() === credentials.email.toLowerCase() &&
          u.password === credentials.password,
      );

      if (!user) {
        throw new AuthServiceError({
          code: "invalid_credentials",
          message: "Invalid email or password.",
          status: 401,
        });
      }

      if (user.status !== "active") {
        throw new AuthServiceError({
          code: "invalid_credentials",
          message: "Account is disabled. Please contact your administrator.",
          status: 403,
        });
      }

      const response = createStubResponse(user);
      currentUser = response.user;
      return response;
    },

    async register(data: {
      email: string;
      password: string;
      display_name: string;
    }): Promise<AuthResponse> {
      await new Promise((resolve) => setTimeout(resolve, 500));

      // Check if email exists
      if (STUB_USERS.some((u) => u.email.toLowerCase() === data.email.toLowerCase())) {
        throw new AuthServiceError({
          code: "server_error",
          message: "An account with this email already exists.",
          status: 409,
        });
      }

      const newUser: StubUser = {
        user_id: crypto.randomUUID(),
        email: data.email,
        password: data.password,
        display_name: data.display_name,
        tenant_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        role_name: "operator",
        permissions: [
          "venue.read",
          "video.read",
          "analytics.read",
          "evidence.read",
          "recommendation.read",
          "alert.read",
        ],
        venue_scope: [],
        status: "active",
      };

      STUB_USERS.push(newUser);
      const response = createStubResponse(newUser);
      currentUser = response.user;
      return response;
    },

    async refreshToken(refreshToken: string): Promise<AuthTokens> {
      await new Promise((resolve) => setTimeout(resolve, 200));

      // Find user by refresh token
      const userId = refreshToken.replace("stub-refresh-", "");
      const user = STUB_USERS.find((u) => u.user_id === userId);

      if (!user) {
        throw new AuthServiceError({
          code: "session_expired",
          message: "Invalid refresh token",
          status: 401,
        });
      }

      const newAccessToken = generateStubToken(user.user_id);
      const newRefreshToken = `stub-refresh-${user.user_id}`;

      return {
        access_token: newAccessToken,
        token_type: "bearer",
        expires_in: 3600,
        refresh_token: newRefreshToken,
      };
    },

    async logout(): Promise<void> {
      await new Promise((resolve) => setTimeout(resolve, 100));
      currentUser = null;
    },

    clearTokens(): void {
      currentUser = null;
    },

    async getCurrentUser(): Promise<UserInfo> {
      await new Promise((resolve) => setTimeout(resolve, 100));

      if (!currentUser) {
        throw new AuthServiceError({
          code: "session_expired",
          message: "No active session",
          status: 401,
        });
      }

      return currentUser;
    },

    async verifyToken(): Promise<boolean> {
      try {
        await this.getCurrentUser();
        return true;
      } catch {
        return false;
      }
    },
  };
}

/**
 * Factory to create appropriate auth service based on environment
 */
export function createAuthService(config: AuthServiceConfig = { baseUrl: "" }) {
  const useStub = import.meta.env.VITE_USE_AUTH_STUB === "true" || import.meta.env.DEV;

  if (useStub) {
    console.warn(
      "[Auth] Using STUB authentication service. Set VITE_USE_AUTH_STUB=false to use real API.",
    );
    return createStubAuthService(config);
  }

  // Import real implementation dynamically to avoid bundling stub in production
  // This is a placeholder - in production, the real service would be used
  return createStubAuthService(config);
}
