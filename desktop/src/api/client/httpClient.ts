/**
 * HTTP Client - Centralized HTTP client for API communication.
 *
 * Single entry point for all HTTP requests to the FastAPI backend.
 * Handles: authentication, retries, timeouts, cancellation, error normalization.
 *
 * Architecture:
 *   React Components
 *       ↓
 *   Feature Services
 *       ↓
 *   HttpClient (this module)
 *       ↓
 *   FastAPI Backend
 */

import { apiConfig } from "./config";
import { ApiErrorClass, createApiError } from "./errors";
import { type HttpClient, type RequestConfig, type ApiResponse } from "../types";
import { useAuthStore } from "@/features/auth/hooks/useAuthStore";

/** Default headers for all requests */
const DEFAULT_HEADERS: Record<string, string> = {
  "Content-Type": "application/json",
  Accept: "application/json",
};

/** Request ID header name */
const REQUEST_ID_HEADER = "x-request-id";

/**
 * Generate a unique request ID for tracing.
 */
function generateRequestId(): string {
  return String(Date.now()) + "-" + Math.random().toString(36).substring(2, 11);
}

/**
 * Get the current access token from auth store.
 * This is the single source of truth for authentication tokens.
 */
function getAccessToken(): string | null {
  // Use the auth store's getAccessToken if available, otherwise read from state
  try {
    const state = useAuthStore.getState();
    return state.tokens?.access_token ?? null;
  } catch {
    return null;
  }
}

/**
 * Build request headers with authentication.
 */
function buildHeaders(
  customHeaders: Record<string, string> = {},
  includeAuth = true,
): Record<string, string> {
  const headers: Record<string, string> = {
    ...DEFAULT_HEADERS,
    ...customHeaders,
  };

  const requestId = generateRequestId();
  headers[REQUEST_ID_HEADER] = requestId;

  if (includeAuth) {
    const token = getAccessToken();
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }
  }

  return headers;
}

/**
 * Sleep utility for retry delays.
 */
function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Calculate exponential backoff delay with jitter.
 */
function calculateBackoff(attempt: number, baseDelay: number): number {
  const exponentialDelay = baseDelay * Math.pow(2, attempt);
  const jitter = Math.random() * 0.3 * exponentialDelay;
  return Math.min(exponentialDelay + jitter, 30000); // Cap at 30 seconds
}

/** HTTP status codes that are retryable */
function isRetryableHttpStatus(status: number): boolean {
  return [429, 500, 502, 503, 504].includes(status);
}

/** Map HTTP status to error code */
function httpStatusToErrorCode(status: number): string {
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
      if (status >= 500) return "SERVER_ERROR";
      return "UNKNOWN";
  }
}

/**
 * Core HTTP client implementation.
 */
export class HttpClientImpl implements HttpClient {
  private baseUrl: string;
  private defaultTimeout: number;
  private maxRetries: number;
  private retryDelay: number;

  constructor(config: {
    baseUrl: string;
    defaultTimeout: number;
    maxRetries: number;
    retryDelay: number;
  }) {
    this.baseUrl = config.baseUrl.replace(/\/$/, "");
    this.defaultTimeout = config.defaultTimeout;
    this.maxRetries = config.maxRetries;
    this.retryDelay = config.retryDelay;
  }

  /**
   * Execute a request with retry logic.
   */
  private async executeWithRetry(
    config: RequestConfig,
    attempt = 0,
  ): Promise<ApiResponse<unknown>> {
    const {
      method,
      url,
      params,
      data,
      headers = {},
      timeout = this.defaultTimeout,
      signal,
      withCredentials = false,
    } = config;

    // Build full URL
    const fullUrl = `${this.baseUrl}${url.startsWith("/") ? url : `/${url}`}`;

    // Build query string from params
    const queryString = params
      ? "?" +
        new URLSearchParams(
          Object.entries(params).reduce<Record<string, string>>((acc, [key, value]) => {
            if (value !== undefined && value !== null) {
              acc[key] = typeof value === "string" ? value : JSON.stringify(value);
            }
            return acc;
          }, {}),
        ).toString()
      : "";

    const fullUrlWithParams = `${fullUrl}${queryString}`;

    // Build headers
    const requestHeaders = buildHeaders(headers);

    // Create abort controller for timeout
    const controller = new AbortController();
    const timeoutId = setTimeout(() => {
      controller.abort();
    }, timeout);

    // Combine signals
    let combinedSignal = controller.signal;
    if (signal) {
      const combinedController = new AbortController();
      signal.addEventListener("abort", () => {
        combinedController.abort();
      });
      controller.signal.addEventListener("abort", () => {
        combinedController.abort();
      });
      combinedSignal = combinedController.signal;
    }

    try {
      const response = await fetch(fullUrlWithParams, {
        method,
        headers: requestHeaders,
        body: data ? JSON.stringify(data) : undefined,
        signal: combinedSignal,
        credentials: withCredentials ? "include" : "omit",
      });

      clearTimeout(timeoutId);

      // Handle successful responses
      if (response.ok) {
        // Handle 204 No Content
        if (response.status === 204) {
          return {
            data: undefined as unknown,
            meta: {
              requestId: response.headers.get("x-request-id") ?? undefined,
              timestamp: new Date().toISOString(),
            },
          };
        }

        // Parse JSON response
        const data = (await response.json()) as unknown;
        return {
          data,
          meta: {
            requestId: response.headers.get("x-request-id") ?? undefined,
            timestamp: new Date().toISOString(),
          },
        };
      }

      // Handle error responses
      let errorMessage = `HTTP ${String(response.status)}: ${response.statusText}`;

      try {
        const errorBody = (await response.json()) as { detail?: unknown };
        if (typeof errorBody === "object") {
          if ("detail" in errorBody) {
            const detail = errorBody.detail;
            if (typeof detail === "string") {
              errorMessage = detail;
            } else if (typeof detail === "object" && detail !== null) {
              // Handle validation errors
              errorMessage = "Validation error";
            }
          }
        }
      } catch {
        // Use status text if no JSON body
      }

      // Check if we should retry
      if (attempt < this.maxRetries && isRetryableHttpStatus(response.status)) {
        const delay = calculateBackoff(attempt, this.retryDelay);
        await sleep(delay);
        return await this.executeWithRetry(config, attempt + 1);
      }

      throw new ApiErrorClass({
        status: response.status,
        code: httpStatusToErrorCode(response.status),
        message: errorMessage,
        requestId: response.headers.get("x-request-id") ?? undefined,
      });
    } catch (error) {
      clearTimeout(timeoutId);

      // Re-throw ApiErrorClass as-is
      if (error instanceof ApiErrorClass) {
        throw error;
      }

      // Handle abort/cancellation
      if (error instanceof DOMException && error.name === "AbortError") {
        throw new ApiErrorClass({
          status: 0,
          code: "TIMEOUT",
          message: "Request was cancelled",
        });
      }

      // Network errors
      if (error instanceof TypeError && error.message.includes("fetch")) {
        throw new ApiErrorClass({
          status: 0,
          code: "NETWORK_ERROR",
          message: "Network error - unable to reach server",
        });
      }

      // Re-throw other errors
      throw createApiError(error);
    }
  }

  /** GET request */
  async get<T>(
    url: string,
    params?: Record<string, unknown>,
    config?: Partial<RequestConfig>,
  ): Promise<ApiResponse<T>> {
    return this.request<T>({ ...config, method: "GET", url, params });
  }

  /** POST request */
  async post<T>(
    url: string,
    data?: unknown,
    config?: Partial<RequestConfig>,
  ): Promise<ApiResponse<T>> {
    return this.request<T>({ ...config, method: "POST", url, data });
  }

  /** PUT request */
  async put<T>(
    url: string,
    data?: unknown,
    config?: Partial<RequestConfig>,
  ): Promise<ApiResponse<T>> {
    return this.request<T>({ ...config, method: "PUT", url, data });
  }

  /** PATCH request */
  async patch<T>(
    url: string,
    data?: unknown,
    config?: Partial<RequestConfig>,
  ): Promise<ApiResponse<T>> {
    return this.request<T>({ ...config, method: "PATCH", url, data });
  }

  /** DELETE request */
  async delete<T>(url: string, config?: Partial<RequestConfig>): Promise<ApiResponse<T>> {
    return this.request<T>({ ...config, method: "DELETE", url });
  }

  /** Generic request method */
  async request<T>(config: RequestConfig): Promise<ApiResponse<T>> {
    const result = await this.executeWithRetry(config);
    return result as ApiResponse<T>;
  }
}

/**
 * Create a configured HTTP client instance.
 * Uses the centralized apiConfig.
 */
export function createHttpClient(): HttpClient {
  return new HttpClientImpl({
    baseUrl: apiConfig.baseUrl,
    defaultTimeout: apiConfig.defaultTimeout,
    maxRetries: apiConfig.maxRetries,
    retryDelay: apiConfig.retryDelay,
  });
}

/** Default singleton instance */
export const httpClient = createHttpClient();

export { httpClient as default };
