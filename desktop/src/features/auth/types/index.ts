/**
 * Authentication Types - Frontend Contract
 *
 * These types define the expected backend authentication contract.
 * MISSING BACKEND CONTRACTS (documented for backend team):
 *
 * 1. POST /auth/login
 *    Request: { email: string, password: string }
 *    Response: { access_token: string, token_type: "bearer", expires_in: number, user: UserInfo }
 *
 * 2. POST /auth/register (if needed)
 *    Request: { email: string, password: string, display_name: string }
 *    Response: { access_token: string, token_type: "bearer", expires_in: number, user: UserInfo }
 *
 * 3. POST /auth/refresh
 *    Request: { refresh_token: string } (or via cookie)
 *    Response: { access_token: string, token_type: "bearer", expires_in: number }
 *
 * 4. POST /auth/logout
 *    Request: (none, uses auth header)
 *    Response: { success: boolean }
 *
 * 5. GET /auth/me
 *    Request: (uses auth header)
 *    Response: UserInfo
 *
 * UserInfo should include:
 * - user_id, email, display_name
 * - tenant_id, role_name, permissions[], venue_scope[]
 * - status (active/disabled)
 *
 * Token Spec (from existing backend):
 * - Algorithm: HS256
 * - Issuer: "hotelops-ai"
 * - Subject: user_id (UUID)
 * - Expiration: 60 minutes (configurable via JWT_EXPIRATION_MINUTES)
 * - Claims: ONLY sub, iat, exp, iss (NO auth claims in token)
 */

export type AuthStatus =
  | "initializing"
  | "authenticating"
  | "authenticated"
  | "unauthenticated"
  | "session_expired"
  | "auth_error";

export interface LoginCredentials {
  email: string;
  password: string;
}

export interface RegisterData {
  email: string;
  password: string;
  display_name: string;
}

export interface AuthTokens {
  access_token: string;
  token_type: "bearer";
  expires_in: number; // seconds
  refresh_token?: string; // if backend implements refresh
}

export interface UserInfo {
  user_id: string;
  email: string;
  display_name: string;
  tenant_id: string;
  role_name: "admin" | "manager" | "operator";
  permissions: string[];
  venue_scope: string[];
  status: "active" | "disabled";
}

export interface AuthResponse {
  tokens: AuthTokens;
  user: UserInfo;
}

export interface AuthError {
  code:
    | "invalid_credentials"
    | "network_error"
    | "server_error"
    | "session_expired"
    | "permission_denied"
    | "unknown";
  message: string;
  status?: number;
}

export interface SessionState {
  status: AuthStatus;
  user: UserInfo | null;
  tokens: AuthTokens | null;
  error: AuthError | null;
  isRestoring: boolean;
}
