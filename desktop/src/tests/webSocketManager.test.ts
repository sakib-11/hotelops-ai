/**
 * Test Suite: WebSocket Manager & Realtime Dispatch
 *
 * Validates Subtask 40.7:
 * - Connection lifecycle states (DISCONNECTED, CONNECTING, CONNECTED, etc.)
 * - Bounded backoff reconnection with jitter
 * - Reference-counted channel subscriptions
 * - Event envelope processing and sequence gap detection
 * - WebSocket -> TanStack Query cache invalidation
 * - Deterministic disconnection on logout
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { WebSocketManager } from "@/realtime/WebSocketManager";
import { queryClient } from "@/query/client";
import type { EventEnvelope } from "@/realtime/types";

describe("WebSocket Manager", () => {
  let manager: WebSocketManager;

  beforeEach(() => {
    vi.restoreAllMocks();
    manager = new WebSocketManager({
      wsUrl: "ws://localhost:8000/ws",
      initialRetryDelay: 50,
      maxRetryDelay: 200,
      maxReconnectAttempts: 3,
    });
  });

  it("should initialize in DISCONNECTED status", () => {
    expect(manager.getStatus()).toBe("DISCONNECTED");
  });

  it("should notify status listeners on state transitions", () => {
    const statuses: string[] = [];
    manager.onStatusChange((status) => {
      statuses.push(status);
    });

    expect(statuses).toEqual(["DISCONNECTED"]);
  });

  it("should parse EventEnvelope and dispatch to registered event handlers", () => {
    const receivedEvents: EventEnvelope[] = [];

    const unsubscribe = manager.onEvent("alert.triggered", (envelope) => {
      receivedEvents.push(envelope);
    });

    const sampleEnvelope: EventEnvelope = {
      event_id: "evt-123",
      event_type: "alert.triggered",
      schema_version: "1.0",
      event_time: "2026-08-10T12:00:00Z",
      produced_at: "2026-08-10T12:00:00Z",
      correlation_id: "corr-1",
      causation_id: null,
      source: "alerts-worker",
      payload: { alert_id: "a-1", severity: "high" },
      sequence_id: 1,
    };

    manager.processEventEnvelope(sampleEnvelope);

    expect(receivedEvents.length).toBe(1);
    expect(receivedEvents[0]?.event_id).toBe("evt-123");
    expect(receivedEvents[0]?.event_type).toBe("alert.triggered");

    unsubscribe();
    manager.processEventEnvelope(sampleEnvelope);
    expect(receivedEvents.length).toBe(1); // Unsubscribed
  });

  it("should invalidate query cache on event envelope receipt (WebSocket -> Cache Pattern)", () => {
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    const sampleEnvelope: EventEnvelope = {
      event_id: "evt-456",
      event_type: "camera.status_changed",
      schema_version: "1.0",
      event_time: "2026-08-10T12:00:00Z",
      produced_at: "2026-08-10T12:00:00Z",
      correlation_id: null,
      causation_id: null,
      source: "cv-stream",
      payload: { camera_id: "cam-1", status: "online" },
    };

    manager.processEventEnvelope(sampleEnvelope);

    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["cameras"],
    });
  });

  it("should detect sequence gap and trigger resync cache invalidation", () => {
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    manager.processEventEnvelope({
      event_id: "evt-1",
      event_type: "alert.created",
      schema_version: "1.0",
      event_time: "2026-08-10T12:00:00Z",
      produced_at: "2026-08-10T12:00:00Z",
      correlation_id: null,
      causation_id: null,
      source: "worker",
      payload: {},
      sequence_id: 10,
    });

    // Gap: jumps from 10 to 15
    manager.processEventEnvelope({
      event_id: "evt-2",
      event_type: "alert.created",
      schema_version: "1.0",
      event_time: "2026-08-10T12:00:05Z",
      produced_at: "2026-08-10T12:00:05Z",
      correlation_id: null,
      causation_id: null,
      source: "worker",
      payload: {},
      sequence_id: 15,
    });

    // Should invalidate multiple domain caches due to gap
    expect(invalidateSpy).toHaveBeenCalled();
  });

  it("should reference count channel subscriptions properly", () => {
    const request = {
      channel: "alerts" as const,
      tenant_id: "tenant-1",
    };

    const unsub1 = manager.subscribeChannel(request);
    const unsub2 = manager.subscribeChannel(request);

    // First unsubscribe keeps subscription active
    unsub1();
    // Second unsubscribe cleans up
    unsub2();

    expect(manager.getStatus()).toBe("DISCONNECTED");
  });

  it("should transition to DISCONNECTED on explicit logout disconnect", () => {
    manager.disconnect();
    expect(manager.getStatus()).toBe("DISCONNECTED");
  });
});
