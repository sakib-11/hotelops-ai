/**
 * Server-State Cache - Main exports for the query layer.
 *
 * This is the single entry point for all server-state cache functionality.
 *
 * Architecture:
 *   React Components
 *       ↓
 *   Feature Hooks (useHealth, useAuth, useSystem, etc.)
 *       ↓
 *   Query Hooks (useQuery, useMutation)
 *       ↓
 *   API Services (health, auth, system)
 *       ↓
 *   Typed HTTP Client
 *       ↓
 *   FastAPI Backend
 */

export { queryClient, createQueryClient } from "./client";
export { queryKeys, invalidateDomain, getBaseKey } from "./keys";
export { QueryProvider, queryClient as defaultClient } from "./Provider";
export * from "./hooks";
