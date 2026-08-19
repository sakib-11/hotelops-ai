/**
 * AppError Types - Centralized application-level error representation.
 *
 * This is the single application-level error model that orchestrates
 * error handling across Task 40.5 (API), 40.6 (Query), 40.7 (WebSocket),
 * and other subsystems.
 *
 * Do NOT duplicate this model elsewhere.
 */

/**
 * Failure categories - orchestrates across all subsystems.
 * Each category maps to specific handling behavior.
 */
export type FailureCategory =
  | "USER_INPUT" // CATEGORY 1: invalid form, missing field, invalid param
  | "AUTHENTICATION" // CATEGORY 2: session expired, unauthorized, auth unavailable
  | "AUTHORIZATION" // CATEGORY 3: insufficient permission, forbidden
  | "NETWORK" // CATEGORY 4: backend unreachable, timeout, DNS failure
  | "API" // CATEGORY 5: 400, 404, 409, 422, 500, 502, 503
  | "WEBSOCKET" // CATEGORY 6: disconnected, reconnecting, malformed event
  | "CLIENT_RUNTIME" // CATEGORY 7: React crash, JS exception, render failure
  | "APPLICATION"; // CATEGORY 8: Tauri init, native capability, filesystem

/**
 * Recoverability classification - determines UI behavior.
 */
export type Recoverability =
  | "RECOVERABLE" // Auto-retry works (temp network, 503, WS disconnect)
  | "USER_ACTION_REQUIRED" // User must act (invalid creds, 403, 422, session expired)
  | "NON_RECOVERABLE"; // Fatal (app init failure, corrupted state, unrecoverable runtime)

/**
 * Error severity - determines UI presentation.
 */
export type ErrorSeverity =
  | "INFO" // Background refresh failed, cached data usable
  | "WARNING" // WS disconnected, degraded mode
  | "ERROR" // API request failed, operation blocked
  | "CRITICAL"; // App cannot initialize, fatal startup

/**
 * Normalized application-level error.
 *
 * This is the single error model that ALL subsystems translate to.
 * Components receive this type, never raw errors.
 */
export interface AppError {
  readonly category: FailureCategory;
  readonly code: string; // Specific error code (e.g., "UNAUTHENTICATED")
  readonly message: string; // Safe user-facing message
  readonly severity: ErrorSeverity;
  readonly recoverability: Recoverability;
  readonly requestId?: string; // For debugging/support
  readonly timestamp: string; // ISO 8601
  readonly cause?: unknown; // Original error (never exposed to UI)
  readonly context?: Record<string, unknown>; // Additional context (route, operation, etc.)
  readonly technicalDetails?: string; // Dev-only details (stack trace, etc.)
}

/**
 * Maps API error codes to failure categories.
 */
export function apiErrorCodeToCategory(code: string): FailureCategory {
  switch (code) {
    case "UNAUTHENTICATED":
    case "SESSION_EXPIRED":
      return "AUTHENTICATION";
    case "FORBIDDEN":
    case "PERMISSION_DENIED":
      return "AUTHORIZATION";
    case "NOT_FOUND":
    case "CONFLICT":
    case "VALIDATION_ERROR":
    case "RATE_LIMITED":
    case "SERVER_ERROR":
    case "SERVICE_UNAVAILABLE":
    case "MALFORMED_RESPONSE":
      return "API";
    case "NETWORK_ERROR":
    case "TIMEOUT":
      return "NETWORK";
    default:
      return "API";
  }
}

/**
 * Maps failure category to default recoverability.
 */
export function categoryToRecoverability(category: FailureCategory, code?: string): Recoverability {
  // USER_ACTION_REQUIRED for these specific codes regardless of category
  if (code) {
    switch (code) {
      case "UNAUTHENTICATED":
      case "SESSION_EXPIRED":
      case "FORBIDDEN":
      case "PERMISSION_DENIED":
      case "VALIDATION_ERROR":
      case "CONFLICT":
        return "USER_ACTION_REQUIRED";
    }
  }

  switch (category) {
    case "NETWORK":
    case "WEBSOCKET":
      return "RECOVERABLE";
    case "AUTHENTICATION":
    case "AUTHORIZATION":
      return "USER_ACTION_REQUIRED";
    case "API":
      if (code === "RATE_LIMITED" || code === "SERVICE_UNAVAILABLE") {
        return "RECOVERABLE";
      }
      return "USER_ACTION_REQUIRED";
    case "CLIENT_RUNTIME":
    case "APPLICATION":
      return "NON_RECOVERABLE";
    case "USER_INPUT":
      return "USER_ACTION_REQUIRED";
    default:
      return "USER_ACTION_REQUIRED";
  }
}

/**
 * Maps failure category to default severity.
 */
export function categoryToSeverity(
  category: FailureCategory,
  recoverability: Recoverability,
): ErrorSeverity {
  if (recoverability === "NON_RECOVERABLE") return "CRITICAL";
  if (category === "NETWORK" || category === "WEBSOCKET") return "WARNING";
  if (category === "CLIENT_RUNTIME" || category === "APPLICATION") return "CRITICAL";
  return "ERROR";
}
