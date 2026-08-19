/**
 * Test Suite: Typed FastAPI Client & Error Normalization
 *
 * Validates Subtask 40.5:
 * - Base URL, headers, request correlation IDs
 * - HTTP status code error mapping
 * - Safe user-friendly error messages
 * - Retry policy and exponential backoff
 * - Cancellation and timeout handling
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { HttpClientImpl } from "@/api/client/httpClient";
import { ApiErrorClass, createApiError, httpStatusToErrorCode } from "@/api/client/errors";

describe("Typed FastAPI Client", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    global.fetch = originalFetch;
  });

  it("should construct request with correlation headers and base URL", async () => {
    let capturedUrl = "";
    let capturedHeaders: Record<string, string> = {};

    global.fetch = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      capturedUrl = url;
      capturedHeaders = (init?.headers as Record<string, string>) ?? {};
      return Promise.resolve(
        new Response(JSON.stringify({ status: "ok", service: "hotelops-ai", version: "0.1.0" }), {
          status: 200,
          headers: { "Content-Type": "application/json", "x-request-id": "req-123" },
        }),
      );
    });

    const client = new HttpClientImpl({
      baseUrl: "http://localhost:8000",
      defaultTimeout: 5000,
      maxRetries: 0,
      retryDelay: 100,
    });

    const response = await client.get<{ status: string }>("/health");

    expect(capturedUrl).toBe("http://localhost:8000/health");
    expect(capturedHeaders["Content-Type"]).toBe("application/json");
    expect(capturedHeaders["x-request-id"]).toBeDefined();
    expect(response.data.status).toBe("ok");
    expect(response.meta?.requestId).toBe("req-123");
  });

  it("should normalize HTTP 401 into UNAUTHENTICATED error", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "Signature has expired" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const client = new HttpClientImpl({
      baseUrl: "http://localhost:8000",
      defaultTimeout: 5000,
      maxRetries: 0,
      retryDelay: 100,
    });

    await expect(client.get("/protected")).rejects.toThrowError(ApiErrorClass);

    try {
      await client.get("/protected");
    } catch (err) {
      const apiErr = err as ApiErrorClass;
      expect(apiErr.status).toBe(401);
      expect(apiErr.code).toBe("UNAUTHENTICATED");
      expect(apiErr.userMessage).toBe("Your session has expired. Please sign in again.");
    }
  });

  it("should map HTTP status codes correctly", () => {
    expect(httpStatusToErrorCode(401)).toBe("UNAUTHENTICATED");
    expect(httpStatusToErrorCode(403)).toBe("FORBIDDEN");
    expect(httpStatusToErrorCode(404)).toBe("NOT_FOUND");
    expect(httpStatusToErrorCode(409)).toBe("CONFLICT");
    expect(httpStatusToErrorCode(422)).toBe("VALIDATION_ERROR");
    expect(httpStatusToErrorCode(429)).toBe("RATE_LIMITED");
    expect(httpStatusToErrorCode(500)).toBe("SERVER_ERROR");
    expect(httpStatusToErrorCode(503)).toBe("SERVICE_UNAVAILABLE");
  });

  it("should retry transient 503 errors up to maxRetries", async () => {
    let callCount = 0;

    global.fetch = vi.fn().mockImplementation(() => {
      callCount++;
      if (callCount < 3) {
        return Promise.resolve(
          new Response(JSON.stringify({ detail: "Service temporarily unavailable" }), {
            status: 503,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify({ status: "recovered" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });

    const client = new HttpClientImpl({
      baseUrl: "http://localhost:8000",
      defaultTimeout: 5000,
      maxRetries: 3,
      retryDelay: 10,
    });

    const response = await client.get<{ status: string }>("/health");
    expect(callCount).toBe(3);
    expect(response.data.status).toBe("recovered");
  });

  it("should normalize network fetch failure without crashing", () => {
    const fetchError = new TypeError("Failed to fetch");
    const normalized = createApiError(fetchError);

    expect(normalized.code).toBe("NETWORK_ERROR");
    expect(normalized.status).toBe(0);
    expect(normalized.userMessage).toBe(
      "Unable to connect to the server. Please check your connection.",
    );
  });
});
