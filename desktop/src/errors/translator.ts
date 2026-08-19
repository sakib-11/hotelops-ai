/**
 * Error Translator - Centralized Error Normalization & Deduplication
 *
 * Implements Subtask 40.8:
 * - Translates raw errors into normalized AppError
 * - Implements deduplication to prevent flood of identical errors
 * - Sanitizes messages for safe presentation in user UI
 */

import { ApiErrorClass } from "@/api/client/errors";
import type { AppError, FailureCategory, Recoverability, ErrorSeverity } from "./types";
import { apiErrorCodeToCategory, categoryToRecoverability, categoryToSeverity } from "./types";

/** Safe, user-friendly fallback messages */
const USER_MESSAGES: Record<FailureCategory, string> = {
  USER_INPUT: "Please check your input and try again.",
  AUTHENTICATION: "Your session has expired. Please sign in again.",
  AUTHORIZATION: "You do not have permission to perform this action.",
  NETWORK: "Unable to connect to the HotelOps AI service. Please check your connection.",
  API: "The HotelOps AI service is temporarily unavailable. Please try again later.",
  WEBSOCKET: "Real-time updates are temporarily disconnected. Attempting to reconnect.",
  CLIENT_RUNTIME: "An unexpected display error occurred. Please refresh the page.",
  APPLICATION: "Application error occurred. Please restart the application.",
};

class ErrorDeduplicator {
  private recentErrors = new Map<string, number>();
  private readonly cooldownMs: number;

  constructor(cooldownMs = 5000) {
    this.cooldownMs = cooldownMs;
  }

  /**
   * Returns true if this error signature is new or past its cooldown window.
   */
  public shouldReport(signature: string): boolean {
    const now = Date.now();
    const lastSeen = this.recentErrors.get(signature);

    if (lastSeen && now - lastSeen < this.cooldownMs) {
      return false; // Deduplicated
    }

    this.recentErrors.set(signature, now);

    // Prune stale entries
    if (this.recentErrors.size > 100) {
      for (const [key, timestamp] of this.recentErrors.entries()) {
        if (now - timestamp > this.cooldownMs * 2) {
          this.recentErrors.delete(key);
        }
      }
    }

    return true;
  }

  public clear(): void {
    this.recentErrors.clear();
  }
}

export const errorDeduplicator = new ErrorDeduplicator();

/**
 * Translate any error source into a normalized AppError.
 */
export function translateError(source: unknown, context?: Record<string, unknown>): AppError {
  const timestamp = new Date().toISOString();

  // 1. ApiErrorClass instance
  if (source instanceof ApiErrorClass) {
    const category = apiErrorCodeToCategory(source.code);
    const recoverability = categoryToRecoverability(category, source.code);
    const severity = categoryToSeverity(category, recoverability);

    return {
      category,
      code: source.code,
      message: source.userMessage || USER_MESSAGES[category],
      severity,
      recoverability,
      requestId: source.requestId,
      timestamp,
      cause: source,
      context,
      technicalDetails: import.meta.env.DEV ? source.message : undefined,
    };
  }

  // 2. Fetch network error
  if (source instanceof TypeError && source.message.toLowerCase().includes("fetch")) {
    return {
      category: "NETWORK",
      code: "NETWORK_UNAVAILABLE",
      message: USER_MESSAGES.NETWORK,
      severity: "WARNING",
      recoverability: "RECOVERABLE",
      timestamp,
      cause: source,
      context,
      technicalDetails: import.meta.env.DEV ? source.message : undefined,
    };
  }

  // 3. Timeout / Abort error
  if (source instanceof DOMException && source.name === "AbortError") {
    return {
      category: "NETWORK",
      code: "TIMEOUT",
      message: "The request timed out. Please try again.",
      severity: "WARNING",
      recoverability: "RECOVERABLE",
      timestamp,
      cause: source,
      context,
      technicalDetails: import.meta.env.DEV ? source.message : undefined,
    };
  }

  // 4. Standard JavaScript Error
  if (source instanceof Error) {
    const category: FailureCategory = "CLIENT_RUNTIME";
    const recoverability: Recoverability = "NON_RECOVERABLE";
    const severity: ErrorSeverity = "CRITICAL";

    return {
      category,
      code: source.name || "CLIENT_ERROR",
      message: USER_MESSAGES.CLIENT_RUNTIME,
      severity,
      recoverability,
      timestamp,
      cause: source,
      context,
      technicalDetails: import.meta.env.DEV
        ? `${source.message}\n${source.stack ?? ""}`
        : undefined,
    };
  }

  // 5. Unknown error
  return {
    category: "APPLICATION",
    code: "UNKNOWN_ERROR",
    message: "Something went wrong. Please try again.",
    severity: "ERROR",
    recoverability: "USER_ACTION_REQUIRED",
    timestamp,
    cause: source,
    context,
    technicalDetails: import.meta.env.DEV ? String(source) : undefined,
  };
}

/**
 * Generates a unique signature for an AppError to allow deduplication.
 */
export function getErrorSignature(error: AppError): string {
  return `${error.category}:${error.code}:${error.message}`;
}
