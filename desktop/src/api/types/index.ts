/**
 * API Types - Centralized type definitions for the HotelOps AI API client.
 *
 * These types represent the actual FastAPI backend contract.
 * Do not invent types that don't exist in the backend.
 */

// =============================================================================
// Base Types
// =============================================================================

/** Base API response wrapper */
export interface ApiResponse<T> {
  data: T;
  meta?: ResponseMeta;
}

/** Response metadata */
export interface ResponseMeta {
  requestId?: string;
  timestamp: string;
  version?: string;
}

/** Standard API error response */
export interface ApiError {
  status: number;
  code: string;
  message: string;
  requestId?: string;
  details?: Record<string, unknown>;
}

/** Pagination parameters */
export interface PaginationParams {
  limit?: number;
  offset?: number;
  cursor?: string;
}

/** Paginated response */
export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  hasMore: boolean;
  nextCursor?: string;
}

// =============================================================================
// Health Types (from /health and /ready endpoints)
// =============================================================================

export type DependencyStatus = "ok" | "failed" | "timeout";
export type OverallStatus = "ready" | "not_ready";

export interface DependencyResult {
  status: DependencyStatus;
}

export interface HealthResponse {
  status: string;
  service: string;
  version: string;
}

export interface ReadinessResponse {
  status: OverallStatus;
  dependencies: Record<string, DependencyResult>;
}

// =============================================================================
// Auth Types (from Task 40.4)
// =============================================================================

export interface LoginCredentials {
  email: string;
  password: string;
}

export interface AuthTokens {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
  refresh_token?: string;
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

// =============================================================================
// Identity/Contract Types (mirrored from backend contracts)
// =============================================================================

/** Canonical identifier types */
export type TenantId = string & { readonly __brand: unique symbol };
export type VenueId = string & { readonly __brand: unique symbol };
export type UserId = string & { readonly __brand: unique symbol };
export type RoleId = string & { readonly __brand: unique symbol };
export type MembershipId = string & { readonly __brand: unique symbol };

export type TenantStatus = "active" | "suspended" | "disabled";
export type VenueStatus = "active" | "inactive";
export type UserStatus = "active" | "disabled";
export type RoleName = "admin" | "manager" | "operator";
export type MembershipStatus = "active" | "inactive";
export type MembershipScope = "all_venues" | "specific_venues";

export type Permission =
  | "venue.read"
  | "venue.manage"
  | "video.read"
  | "video.analyze"
  | "analytics.read"
  | "evidence.read"
  | "recommendation.read"
  | "recommendation.manage"
  | "alert.read"
  | "alert.manage"
  | "user.read"
  | "user.manage"
  | "membership.read"
  | "membership.manage";

/** Server-constructed immutable authorization context */
export interface ActorContext {
  actor_id: UserId;
  tenant_id: TenantId;
  role_name: RoleName;
  permissions: Permission[];
  venue_scope: VenueId[];
  authenticated_at: string;
  active: boolean;

  has_permission(permission: Permission): boolean;
  has_venue_access(venue_id: VenueId): boolean;
  is_admin(): boolean;
}

// =============================================================================
// Realtime Types (WebSocket)
// =============================================================================

export type ChannelResourceType =
  "video.feed" | "analytics" | "alerts" | "evidence" | "recommendations" | "system";

export interface ConnectionState {
  connection_id: string;
  actor: ActorContext;
  connected_at: string;
  subscriptions: ChannelResourceType[];
}

export interface SubscriptionRequest {
  channel: ChannelResourceType;
  tenant_id: string;
  venue_id?: string;
  resource_id?: string;
}

export interface SubscriptionResponse {
  authorized: boolean;
  channel: ChannelResourceType;
  reason?: string;
}

// =============================================================================
// HTTP Client Types
// =============================================================================

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export interface RequestConfig {
  method: HttpMethod;
  url: string;
  params?: Record<string, unknown>;
  data?: unknown;
  headers?: Record<string, string>;
  timeout?: number;
  signal?: AbortSignal;
  withCredentials?: boolean;
}

export interface HttpClient {
  request<T>(config: RequestConfig): Promise<ApiResponse<T>>;
  get<T>(
    url: string,
    params?: Record<string, unknown>,
    config?: Partial<RequestConfig>,
  ): Promise<ApiResponse<T>>;
  post<T>(url: string, data?: unknown, config?: Partial<RequestConfig>): Promise<ApiResponse<T>>;
  put<T>(url: string, data?: unknown, config?: Partial<RequestConfig>): Promise<ApiResponse<T>>;
  patch<T>(url: string, data?: unknown, config?: Partial<RequestConfig>): Promise<ApiResponse<T>>;
  delete<T>(url: string, config?: Partial<RequestConfig>): Promise<ApiResponse<T>>;
}

// =============================================================================
// API Client Configuration
// =============================================================================

export interface ApiClientConfig {
  baseUrl: string;
  defaultTimeout: number;
  maxRetries: number;
  retryDelay: number;
}
