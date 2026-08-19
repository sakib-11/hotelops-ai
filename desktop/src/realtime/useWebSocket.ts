/**
 * WebSocket React Hooks
 *
 * Provides reactive access to WebSocket status and subscriptions.
 */

import { useState, useEffect } from "react";
import { webSocketManager } from "./WebSocketManager";
import type {
  WebSocketConnectionStatus,
  SubscriptionRequest,
  EventEnvelope,
  EventHandler,
} from "./types";
import type { ChannelResourceType } from "@/api/types";

/**
 * Hook to observe the current WebSocket connection status.
 */
export function useWebSocketStatus(): WebSocketConnectionStatus {
  const [status, setStatus] = useState<WebSocketConnectionStatus>(() =>
    webSocketManager.getStatus(),
  );

  useEffect(() => {
    return webSocketManager.onStatusChange(setStatus);
  }, []);

  return status;
}

/**
 * Hook to subscribe a component to a realtime channel resource.
 * Automatically manages subscription lifecycle on mount/unmount.
 */
export function useChannelSubscription(
  channel: ChannelResourceType,
  tenantId: string | undefined,
  venueId?: string | null,
  resourceId?: string | null,
  enabled = true,
): void {
  useEffect(() => {
    if (!enabled || !tenantId) return;

    const request: SubscriptionRequest = {
      channel,
      tenant_id: tenantId,
      venue_id: venueId,
      resource_id: resourceId,
    };

    return webSocketManager.subscribeChannel(request);
  }, [channel, tenantId, venueId, resourceId, enabled]);
}

/**
 * Hook to listen for realtime events of a specific type.
 */
export function useRealtimeEvents<T = unknown>(
  eventType: string,
  handler: EventHandler<T>,
  enabled = true,
): void {
  useEffect(() => {
    if (!enabled) return;
    return webSocketManager.onEvent(eventType, handler);
  }, [eventType, handler, enabled]);
}

export type { WebSocketConnectionStatus, EventEnvelope };
