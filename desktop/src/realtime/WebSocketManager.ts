/**
 * WebSocket Manager - Centralized Real-time Connection Manager
 *
 * Implements Subtask 40.7:
 * - Single WebSocket connection for the desktop client
 * - Deterministic connection state machine
 * - Bounded exponential backoff with jitter for reconnection
 * - Reference-counted channel subscriptions
 * - Authoritative event envelope parsing & safe dispatching
 * - WebSocket -> TanStack Query cache invalidation
 * - Deterministic cleanup on logout
 */

import { apiConfig } from "@/api/client/config";
import { queryClient } from "@/query/client";
import { queryKeys } from "@/query/keys";
import type {
  WebSocketConnectionStatus,
  EventEnvelope,
  SubscriptionRequest,
  ClientMessage,
  ServerMessage,
  WebSocketManagerConfig,
  EventHandler,
  StatusListener,
} from "./types";
import type { ChannelResourceType } from "@/api/types";

const DEFAULT_INITIAL_DELAY = 1000; // 1s
const DEFAULT_MAX_DELAY = 30000; // 30s
const DEFAULT_MAX_ATTEMPTS = 10;
const DEFAULT_HEARTBEAT_INTERVAL = 30000; // 30s

export class WebSocketManager {
  private socket: WebSocket | null = null;
  private status: WebSocketConnectionStatus = "DISCONNECTED";
  private token: string | null = null;
  private explicitDisconnect = false;
  private reconnectAttempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;

  // Active channel subscription requests and reference counts
  private activeSubscriptions = new Map<ChannelResourceType, SubscriptionRequest>();
  private subscriptionRefCounts = new Map<ChannelResourceType, number>();

  // Event handlers by event_type or '*' (wildcard)
  private eventHandlers = new Map<string, Set<EventHandler>>();

  // Status listeners
  private statusListeners = new Set<StatusListener>();

  // Sequence tracking
  private lastSequenceId: number | null = null;

  private readonly config: Required<WebSocketManagerConfig>;

  constructor(config: WebSocketManagerConfig = {}) {
    const defaultWsUrl = this.deriveDefaultWsUrl();
    this.config = {
      wsUrl: config.wsUrl ?? (import.meta.env.VITE_WS_URL || defaultWsUrl),
      initialRetryDelay: config.initialRetryDelay ?? DEFAULT_INITIAL_DELAY,
      maxRetryDelay: config.maxRetryDelay ?? DEFAULT_MAX_DELAY,
      maxReconnectAttempts: config.maxReconnectAttempts ?? DEFAULT_MAX_ATTEMPTS,
      heartbeatInterval: config.heartbeatInterval ?? DEFAULT_HEARTBEAT_INTERVAL,
    };
  }

  private deriveDefaultWsUrl(): string {
    try {
      const httpUrl = new URL(apiConfig.baseUrl);
      const protocol = httpUrl.protocol === "https:" ? "wss:" : "ws:";
      return `${protocol}//${httpUrl.host}/ws`;
    } catch {
      return "ws://localhost:8000/ws";
    }
  }

  /**
   * Get current connection status.
   */
  public getStatus(): WebSocketConnectionStatus {
    return this.status;
  }

  /**
   * Subscribe to connection status changes.
   */
  public onStatusChange(listener: StatusListener): () => void {
    this.statusListeners.add(listener);
    listener(this.status);
    return () => {
      this.statusListeners.delete(listener);
    };
  }

  private setStatus(newStatus: WebSocketConnectionStatus): void {
    if (this.status === newStatus) return;
    this.status = newStatus;
    for (const listener of this.statusListeners) {
      try {
        listener(newStatus);
      } catch (err) {
        console.error("[WebSocketManager] Error in status listener:", err);
      }
    }
  }

  /**
   * Connect to the WebSocket server using the provided JWT access token.
   */
  public connect(token: string): void {
    this.token = token;
    this.explicitDisconnect = false;

    if (
      this.status === "CONNECTED" ||
      this.status === "CONNECTING" ||
      this.status === "RECONNECTING"
    ) {
      return;
    }

    this.setStatus(this.reconnectAttempt > 0 ? "RECONNECTING" : "CONNECTING");
    this.createSocket();
  }

  /**
   * Create the underlying WebSocket instance.
   */
  private createSocket(): void {
    if (!this.token) {
      this.setStatus("DISCONNECTED");
      return;
    }

    try {
      const url = new URL(this.config.wsUrl);
      url.searchParams.set("token", this.token);

      this.socket = new WebSocket(url.toString());

      this.socket.onopen = this.handleOpen.bind(this);
      this.socket.onmessage = this.handleMessage.bind(this);
      this.socket.onerror = this.handleError.bind(this);
      this.socket.onclose = this.handleClose.bind(this);
    } catch (err) {
      console.error("[WebSocketManager] Failed to construct WebSocket:", err);
      this.setStatus("ERROR");
      this.scheduleReconnect();
    }
  }

  private handleOpen(): void {
    this.setStatus("CONNECTED");
    this.reconnectAttempt = 0;
    this.clearReconnectTimer();
    this.startHeartbeat();

    // Re-subscribe to all active channels
    for (const request of this.activeSubscriptions.values()) {
      this.send({
        type: "subscribe",
        subscription: request,
      });
    }
  }

  private handleMessage(event: MessageEvent): void {
    try {
      const rawData = typeof event.data === "string" ? event.data : "";
      if (!rawData) return;

      const message: ServerMessage = JSON.parse(rawData);

      switch (message.type) {
        case "pong":
          // Heartbeat response
          break;

        case "subscription_ack":
          if (!message.response.authorized) {
            console.warn(
              `[WebSocketManager] Subscription rejected for ${message.response.channel}: ${message.response.reason ?? "Unauthorized"}`,
            );
          }
          break;

        case "event":
          this.processEventEnvelope(message.envelope);
          break;

        case "error":
          console.warn("[WebSocketManager] Server error message:", message.message);
          break;
      }
    } catch (err) {
      console.error("[WebSocketManager] Failed to parse message:", err);
    }
  }

  /**
   * Process a received EventEnvelope.
   * Handles sequence gaps, triggers cache invalidations, and notifies subscribers.
   */
  public processEventEnvelope(envelope: EventEnvelope): void {
    if (!envelope?.event_type) return;

    // Sequence tracking and gap detection
    if (typeof envelope.sequence_id === "number") {
      if (this.lastSequenceId !== null && envelope.sequence_id > this.lastSequenceId + 1) {
        console.warn(
          `[WebSocketManager] Sequence gap detected: expected ${this.lastSequenceId + 1}, got ${envelope.sequence_id}. Invalidating caches.`,
        );
        this.invalidateAllDomainCaches();
      }
      this.lastSequenceId = envelope.sequence_id;
    }

    // 1. WebSocket -> Cache Invalidation Pattern (REST is authoritative)
    this.invalidateAffectedQuery(envelope.event_type);

    // 2. Dispatch to registered event listeners
    const specificHandlers = this.eventHandlers.get(envelope.event_type);
    if (specificHandlers) {
      for (const handler of specificHandlers) {
        try {
          handler(envelope);
        } catch (err) {
          console.error(
            `[WebSocketManager] Error in event handler for ${envelope.event_type}:`,
            err,
          );
        }
      }
    }

    const wildcardHandlers = this.eventHandlers.get("*");
    if (wildcardHandlers) {
      for (const handler of wildcardHandlers) {
        try {
          handler(envelope);
        } catch (err) {
          console.error("[WebSocketManager] Error in wildcard handler:", err);
        }
      }
    }
  }

  /**
   * Invalidate affected TanStack Query caches based on event domain.
   */
  private invalidateAffectedQuery(eventType: string): void {
    const domain = eventType.split(".")[0]?.toLowerCase();

    switch (domain) {
      case "camera":
      case "video":
        queryClient.invalidateQueries({ queryKey: queryKeys.cameras.all() });
        break;
      case "alert":
        queryClient.invalidateQueries({ queryKey: queryKeys.alerts.all() });
        break;
      case "event":
        queryClient.invalidateQueries({ queryKey: queryKeys.events.all() });
        break;
      case "evidence":
        queryClient.invalidateQueries({ queryKey: queryKeys.evidence.all() });
        break;
      case "analytics":
        queryClient.invalidateQueries({ queryKey: queryKeys.analytics.all() });
        break;
      case "health":
      case "system":
        queryClient.invalidateQueries({ queryKey: queryKeys.health.all() });
        break;
    }
  }

  private invalidateAllDomainCaches(): void {
    queryClient.invalidateQueries({ queryKey: queryKeys.cameras.all() });
    queryClient.invalidateQueries({ queryKey: queryKeys.alerts.all() });
    queryClient.invalidateQueries({ queryKey: queryKeys.events.all() });
    queryClient.invalidateQueries({ queryKey: queryKeys.evidence.all() });
    queryClient.invalidateQueries({ queryKey: queryKeys.analytics.all() });
    queryClient.invalidateQueries({ queryKey: queryKeys.health.all() });
  }

  private handleError(err: Event): void {
    console.warn("[WebSocketManager] WebSocket error:", err);
    this.setStatus("ERROR");
  }

  private handleClose(event: CloseEvent): void {
    this.stopHeartbeat();
    this.socket = null;

    if (this.explicitDisconnect) {
      this.setStatus("DISCONNECTED");
      return;
    }

    // If closed due to auth failure (code 4001, 4003, or 1008 policy violation)
    if (event.code === 1008 || event.code === 4001 || event.code === 4003) {
      console.warn("[WebSocketManager] Closed due to authentication failure. Halting reconnect.");
      this.setStatus("DISCONNECTED");
      return;
    }

    this.scheduleReconnect();
  }

  /**
   * Schedule reconnection with bounded exponential backoff and jitter.
   */
  private scheduleReconnect(): void {
    if (this.explicitDisconnect || !this.token) {
      this.setStatus("DISCONNECTED");
      return;
    }

    if (this.reconnectAttempt >= this.config.maxReconnectAttempts) {
      console.warn("[WebSocketManager] Max reconnect attempts reached.");
      this.setStatus("ERROR");
      return;
    }

    this.setStatus("RECONNECTING");
    this.reconnectAttempt++;

    // Calculate delay: base * 2^attempt + jitter (±20%)
    const exponential = this.config.initialRetryDelay * Math.pow(2, this.reconnectAttempt - 1);
    const capped = Math.min(exponential, this.config.maxRetryDelay);
    const jitter = (Math.random() * 0.4 - 0.2) * capped; // ±20%
    const delay = Math.max(500, Math.floor(capped + jitter));

    this.clearReconnectTimer();
    this.reconnectTimer = setTimeout(() => {
      this.createSocket();
    }, delay);
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      if (this.socket && this.socket.readyState === WebSocket.OPEN) {
        this.send({ type: "ping", timestamp: new Date().toISOString() });
      }
    }, this.config.heartbeatInterval);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  /**
   * Subscribe a component to a channel resource. Reference-counted.
   */
  public subscribeChannel(request: SubscriptionRequest): () => void {
    const { channel } = request;
    const currentCount = this.subscriptionRefCounts.get(channel) || 0;

    this.subscriptionRefCounts.set(channel, currentCount + 1);
    this.activeSubscriptions.set(channel, request);

    if (currentCount === 0 && this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.send({ type: "subscribe", subscription: request });
    }

    return () => {
      const count = this.subscriptionRefCounts.get(channel) || 0;
      if (count <= 1) {
        this.subscriptionRefCounts.delete(channel);
        this.activeSubscriptions.delete(channel);
        if (this.socket && this.socket.readyState === WebSocket.OPEN) {
          this.send({ type: "unsubscribe", channel });
        }
      } else {
        this.subscriptionRefCounts.set(channel, count - 1);
      }
    };
  }

  /**
   * Register a handler for events of type `eventType` (or `*` for all events).
   */
  public onEvent<T = unknown>(eventType: string, handler: EventHandler<T>): () => void {
    let handlers = this.eventHandlers.get(eventType);
    if (!handlers) {
      handlers = new Set();
      this.eventHandlers.set(eventType, handlers);
    }
    handlers.add(handler as EventHandler);

    return () => {
      const set = this.eventHandlers.get(eventType);
      if (set) {
        set.delete(handler as EventHandler);
        if (set.size === 0) {
          this.eventHandlers.delete(eventType);
        }
      }
    };
  }

  /**
   * Send a typed client message over the WebSocket.
   */
  public send(message: ClientMessage): void {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(message));
    }
  }

  /**
   * Disconnect the WebSocket and cancel all reconnect timers.
   * Called explicitly on user logout.
   */
  public disconnect(): void {
    this.explicitDisconnect = true;
    this.token = null;
    this.reconnectAttempt = 0;
    this.clearReconnectTimer();
    this.stopHeartbeat();

    if (this.socket) {
      this.setStatus("CLOSING");
      this.socket.close(1000, "User logged out");
      this.socket = null;
    }

    this.setStatus("DISCONNECTED");
  }
}

/**
 * Singleton WebSocketManager instance for the application.
 */
export const webSocketManager = new WebSocketManager();
export default webSocketManager;
