/**
 * Realtime & WebSocket Types
 *
 * Models authoritative contracts from:
 * - contracts/realtime/models.py
 * - contracts/events/envelope.py
 * - backend/app/infrastructure/auth/websocket.py
 */

import type { ChannelResourceType, TenantId, VenueId } from "@/api/types";

/**
 * Deterministic connection states for WebSocket lifecycle.
 */
export type WebSocketConnectionStatus =
  "DISCONNECTED" | "CONNECTING" | "CONNECTED" | "RECONNECTING" | "CLOSING" | "ERROR";

/**
 * Canonical Event Envelope matching contracts/events/envelope.py.
 */
export interface EventEnvelope<T = unknown> {
  readonly event_id: string;
  readonly event_type: string;
  readonly schema_version: string;
  readonly event_time: string;
  readonly produced_at: string;
  readonly correlation_id: string | null;
  readonly causation_id: string | null;
  readonly source: string;
  readonly payload: T;
  readonly sequence_id?: number;
}

/**
 * Subscription Request matching contracts/realtime/models.py.
 */
export interface SubscriptionRequest {
  readonly channel: ChannelResourceType;
  readonly tenant_id: TenantId | string;
  readonly venue_id?: VenueId | string | null;
  readonly resource_id?: string | null;
}

/**
 * Subscription Response matching contracts/realtime/models.py.
 */
export interface SubscriptionResponse {
  readonly authorized: boolean;
  readonly channel: ChannelResourceType;
  readonly reason?: string | null;
}

/**
 * Client to Server message protocol.
 */
export type ClientMessage =
  | { type: "subscribe"; subscription: SubscriptionRequest }
  | { type: "unsubscribe"; channel: ChannelResourceType }
  | { type: "ping"; timestamp: string };

/**
 * Server to Client message protocol.
 */
export type ServerMessage =
  | { type: "subscription_ack"; response: SubscriptionResponse }
  | { type: "event"; envelope: EventEnvelope }
  | { type: "pong"; timestamp: string }
  | { type: "error"; message: string; code?: string };

/**
 * WebSocket manager configuration.
 */
export interface WebSocketManagerConfig {
  readonly wsUrl?: string;
  readonly initialRetryDelay?: number;
  readonly maxRetryDelay?: number;
  readonly maxReconnectAttempts?: number;
  readonly heartbeatInterval?: number;
}

export type EventHandler<T = unknown> = (envelope: EventEnvelope<T>) => void;
export type StatusListener = (status: WebSocketConnectionStatus) => void;
