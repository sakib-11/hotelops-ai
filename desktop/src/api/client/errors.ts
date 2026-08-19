/**
 * API Errors - Normalized error handling for the API client.
 *
 * All API errors are normalized to this structure regardless of
 * the underlying HTTP status or backend error format.
 */

import { ApiError } from "../types";

/** Error codes matching backend contract */
export type ApiErrorCode =
  | "UNAUTHENTICATED"
  | "FORBIDDEN"
  | "NOT_FOUND"
  | "CONFLICT"
  | "VALIDATION_ERROR"
  | "RATE_LIMITED"
  | "SERVER_ERROR"
  | "SERVICE_UNAVAILABLE"
  | "NETWORK_ERROR"
  | "TIMEOUT"
  | "MALFORMED_RESPONSE"
  | "UNKNOWN";

/** Map HTTP status to error code */
export function httpStatusToErrorCode(status: number): ApiErrorCode {
  switch (status) {
    case 401:
      return "UNAUTHENTICATED";
    case 403:
      return "FORBIDDEN";
    case 404:
      return "NOT_FOUND";
    case 409:
      return "CONFLICT";
    case 422:
      return "VALIDATION_ERROR";
    case 429:
      return "RATE_LIMITED";
    case 500:
      return "SERVER_ERROR";
    case 502:
    case 503:
      return "SERVICE_UNAVAILABLE";
    default:
      if (status >= 500) {
        return "SERVER_ERROR";
      }
      return "UNKNOWN";
  }
}

/** Error messages for user-facing display */
const ERROR_MESSAGES: Record<ApiErrorCode, string> = {
  UNAUTHENTICATED: "Your session has expired. Please sign in again.",
  FORBIDDEN: "You do not have permission to access this resource.",
  NOT_FOUND: "The requested resource was not found.",
  CONFLICT: "A conflict occurred. The resource may have been modified.",
  VALIDATION_ERROR: "Please check your input and try again.",
  RATE_LIMITED: "Too many requests. Please wait a moment and try again.",
  SERVER_ERROR: "An unexpected error occurred. Please try again later.",
  SERVICE_UNAVAILABLE: "The service is temporarily unavailable. Please try again later.",
  NETWORK_ERROR: "Unable to connect to the server. Please check your connection.",
  TIMEOUT: "The request timed out. Please try again.",
  MALFORMED_RESPONSE: "Received an unexpected response from the server.",
  UNKNOWN: "An unexpected error occurred. Please try again.",
};

/** Base API error class */
export class ApiErrorClass extends Error {
  public readonly status: number;
  public readonly code: ApiErrorCode;
  public readonly requestId?: string;
  public readonly details?: Record<string, unknown>;
  public readonly userMessage: string;

  constructor(error: ApiError) {
    super(error.message);
    this.name = "ApiError";
    this.status = error.status;
    this.code = error.code as ApiErrorCode;
    this.requestId = error.requestId;
    this.details = error.details;
    this.userMessage = ERROR_MESSAGES[this.code];
  }
}

/**
 * Create a normalized API error from various sources.
 */
export function createApiError(
  source: unknown,
  _fallbackMessage = "An unexpected error occurred",
): ApiErrorClass {
  // Already normalized
  if (source instanceof ApiErrorClass) {
    return source;
  }

  // Response object (fetch)
  if (source instanceof Response) {
    // For sync version, we can't await, so create a basic error
    return new ApiErrorClass({
      status: source.status,
      code: httpStatusToErrorCode(source.status),
      message: source.statusText || "Request failed",
    });
  }

  // Fetch network error
  if (source instanceof TypeError && source.message.includes("fetch")) {
    return new ApiErrorClass({
      status: 0,
      code: "NETWORK_ERROR",
      message: "Network error - unable to reach server",
    });
  }

  // Abort error (cancellation)
  if (source instanceof DOMException && source.name === "AbortError") {
    return new ApiErrorClass({
      status: 0,
      code: "TIMEOUT",
      message: "Request was cancelled",
    });
  }

  // Generic error
  if (source instanceof Error) {
    return new ApiErrorClass({
      status: 0,
      code: "UNKNOWN",
      message: source.message,
    });
  }

  // Unknown source
  return new ApiErrorClass({
    status: 0,
    code: "UNKNOWN",
    message: "An unexpected error occurred",
  });
}

/**
 * Create a normalized API error from a Response (async version for fetch responses).
 */
export async function createApiErrorFromResponse(response: Response): Promise<ApiErrorClass> {
  const status = response.status;
  const code = httpStatusToErrorCode(status);
  let message = response.statusText;

  // Try to extract detail from response body
  try {
    const body = (await response.clone().json()) as { detail?: string };
    if (body.detail) {
      message = body.detail;
    }
  } catch {
    // Use status text if no JSON body
  }

  const error: import("../types").ApiError = {
    status,
    code,
    message: message || "Request failed",
    requestId: response.headers.get("x-request-id") ?? undefined,
  };

  return new ApiErrorClass(error);
}

/**
 * Check if an error is an authentication error (401)
 * Used for session expiration handling.
 */
export function isAuthenticationError(error: unknown): boolean {
  if (error instanceof ApiErrorClass) {
    return error.code === "UNAUTHENTICATED";
  }
  return false;
}

/**
 * Check if an error is a network/connectivity error.
 */
export function isNetworkError(error: unknown): boolean {
  if (error instanceof ApiErrorClass) {
    return error.code === "NETWORK_ERROR" || error.code === "TIMEOUT";
  }
  if (error instanceof TypeError && error.message.includes("fetch")) {
    return true;
  }
  if (error instanceof DOMException && error.name === "AbortError") {
    return true;
  }
  return false;
}

/**
 * Check if an error is retryable.
 * Retryable: network errors, timeouts, 5xx errors, rate limits.
 */
export function isRetryableError(error: unknown): boolean {
  if (error instanceof ApiErrorClass) {
    return [
      "NETWORK_ERROR",
      "TIMEOUT",
      "SERVER_ERROR",
      "SERVICE_UNAVAILABLE",
      "RATE_LIMITED",
    ].includes(error.code);
  }
  return false;
}

/**
 * Check if error indicates session expiration.
 * Triggers session restoration flow in auth layer.
 */
export function isSessionExpiredError(error: unknown): boolean {
  if (error instanceof ApiErrorClass) {
    return error.code === "UNAUTHENTICATED";
  }
  return false;
}
