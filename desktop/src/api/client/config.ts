/**
 * API Client Configuration
 *
 * Centralized configuration for the API client.
 * Uses environment variables with sensible defaults.
 *
 * IMPORTANT: Never store secrets, passwords, or credentials here.
 * This configuration is bundled with the frontend.
 */

import { ApiClientConfig } from "../types";

// Default configuration values
const DEFAULT_TIMEOUT = 30000; // 30 seconds
const DEFAULT_MAX_RETRIES = 3;
const DEFAULT_RETRY_DELAY = 1000; // 1 second

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_API_TIMEOUT?: string;
  readonly VITE_API_MAX_RETRIES?: string;
  readonly VITE_API_RETRY_DELAY?: string;
}

/**
 * Get the API base URL from environment.
 * Falls back to localhost for development.
 */
function getApiBaseUrl(): string {
  // In production, this should be set via build-time environment variable
  // For development, defaults to localhost FastAPI
  const envUrl = (import.meta.env as ImportMetaEnv).VITE_API_BASE_URL;
  if (envUrl) {
    return envUrl.replace(/\/$/, ""); // Remove trailing slash
  }

  // Development default
  return "http://localhost:8000";
}

/**
 * Get request timeout from environment.
 */
function getRequestTimeout(): number {
  const envTimeout = (import.meta.env as ImportMetaEnv).VITE_API_TIMEOUT;
  if (envTimeout) {
    const parsed = parseInt(envTimeout, 10);
    if (!Number.isNaN(parsed) && parsed > 0) {
      return parsed;
    }
  }
  return DEFAULT_TIMEOUT;
}

/**
 * Get max retries from environment.
 */
function getMaxRetries(): number {
  const envRetries = (import.meta.env as ImportMetaEnv).VITE_API_MAX_RETRIES;
  if (envRetries) {
    const parsed = parseInt(envRetries, 10);
    if (!Number.isNaN(parsed) && parsed >= 0) {
      return parsed;
    }
  }
  return DEFAULT_MAX_RETRIES;
}

/**
 * Get retry delay from environment.
 */
function getRetryDelay(): number {
  const envDelay = (import.meta.env as ImportMetaEnv).VITE_API_RETRY_DELAY;
  if (envDelay) {
    const parsed = parseInt(envDelay, 10);
    if (!Number.isNaN(parsed) && parsed > 0) {
      return parsed;
    }
  }
  return DEFAULT_RETRY_DELAY;
}

/**
 * API client configuration instance.
 * Created once at module load time.
 */
export const apiConfig: Readonly<ApiClientConfig> = Object.freeze({
  baseUrl: getApiBaseUrl(),
  defaultTimeout: getRequestTimeout(),
  maxRetries: getMaxRetries(),
  retryDelay: getRetryDelay(),
});

/**
 * Type for environment-specific config overrides.
 * Used for testing or special environments.
 */
export interface ApiConfigOverrides {
  baseUrl?: string;
  defaultTimeout?: number;
  maxRetries?: number;
  retryDelay?: number;
}

/**
 * Create a new config with overrides.
 * Useful for tests or special environments.
 */
export function createApiConfig(overrides: ApiConfigOverrides = {}): ApiClientConfig {
  return {
    baseUrl: overrides.baseUrl ?? apiConfig.baseUrl,
    defaultTimeout: overrides.defaultTimeout ?? apiConfig.defaultTimeout,
    maxRetries: overrides.maxRetries ?? apiConfig.maxRetries,
    retryDelay: overrides.retryDelay ?? apiConfig.retryDelay,
  };
}

export { apiConfig as default };
