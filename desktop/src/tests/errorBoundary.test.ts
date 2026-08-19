/**
 * Test Suite: Error & Failure Handling Foundation
 *
 * Validates Subtask 40.8:
 * - AppError normalization across failure categories
 * - Recoverability classifications (RECOVERABLE, USER_ACTION_REQUIRED, NON_RECOVERABLE)
 * - Safe user-friendly error messages (no credentials, SQL errors, or internal leaks)
 * - Error deduplication
 */

import { describe, it, expect } from "vitest";
import { translateError, errorDeduplicator, getErrorSignature } from "@/errors/translator";
import { ApiErrorClass } from "@/api/client/errors";

describe("Error & Failure Handling Foundation", () => {
  it("should translate ApiErrorClass into normalized AppError with safe message", () => {
    const apiError = new ApiErrorClass({
      status: 403,
      code: "FORBIDDEN",
      message: "SELECT * FROM internal_credentials WHERE user_id=... denied",
      requestId: "req-999",
    });

    const appError = translateError(apiError);

    expect(appError.category).toBe("AUTHORIZATION");
    expect(appError.recoverability).toBe("USER_ACTION_REQUIRED");
    expect(appError.severity).toBe("ERROR");
    expect(appError.requestId).toBe("req-999");
    expect(appError.message).toBe("You do not have permission to access this resource.");
  });

  it("should classify fetch network failure as recoverable", () => {
    const networkError = new TypeError("Failed to fetch");
    const appError = translateError(networkError);

    expect(appError.category).toBe("NETWORK");
    expect(appError.recoverability).toBe("RECOVERABLE");
    expect(appError.severity).toBe("WARNING");
    expect(appError.message).toContain("HotelOps AI service");
  });

  it("should classify standard JavaScript exceptions as critical runtime failures", () => {
    const jsError = new Error("Cannot read properties of undefined (reading 'map')");
    const appError = translateError(jsError);

    expect(appError.category).toBe("CLIENT_RUNTIME");
    expect(appError.recoverability).toBe("NON_RECOVERABLE");
    expect(appError.severity).toBe("CRITICAL");
  });

  it("should deduplicate repetitive identical errors within cooldown window", () => {
    errorDeduplicator.clear();

    const sampleError = translateError(new TypeError("Failed to fetch"));
    const signature = getErrorSignature(sampleError);

    expect(errorDeduplicator.shouldReport(signature)).toBe(true);
    // Immediate repeat must be suppressed
    expect(errorDeduplicator.shouldReport(signature)).toBe(false);
    expect(errorDeduplicator.shouldReport(signature)).toBe(false);
  });
});
