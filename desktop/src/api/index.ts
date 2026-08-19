/**
 * HotelOps AI API - Main entry point
 *
 * This is the single entry point for all API communication.
 * React components should import from this module, not from client directly.
 *
 * Architecture:
 *   Components
 *       ↓
 *   Feature Hooks/Services (useHealth, useAuth, etc.)
 *       ↓
 *   API Services (health, auth, system)
 *       ↓
 *   HTTP Client
 *       ↓
 *   FastAPI Backend
 */

export * from "./client";
export * from "./services";
export * from "./types";
