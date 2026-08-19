/**
 * Test Suite: Operational Vertical-Slice API Service (Task 18.13)
 *
 * Validates that the desktop reaches the canonical retrieval surface
 * through the shared API client — the only sanctioned path
 * (Tauri → API client → FastAPI). The service functions must:
 *
 * - hit exactly the authorized endpoint for each resource;
 * - encode the resource identity (never a tenant/venue — the backend
 *   derives those from the actor, so the desktop cannot bypass);
 * - return the canonical DTOs as-is (no client-side derivation).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  getOperationalEvent,
  getOperationalFact,
  getEvidenceAvailability,
} from "@/api/services/operational";

const originalFetch = global.fetch;

function mockFetchOnce(payload: unknown, status = 200): ReturnType<typeof vi.fn> {
  const fn = vi.fn().mockResolvedValue(
    new Response(JSON.stringify(payload), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
  global.fetch = fn;
  return fn;
}

const EVENT_ID = "95947620-fc14-5152-bb13-e373706444f7";
const FACT_ID = "376068b5-8c66-589a-ad22-ea586edd14c9";

describe("Operational API Service", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    global.fetch = originalFetch;
  });

  it("should fetch the occupancy event from the authorized endpoint", async () => {
    const payload = { event_id: EVENT_ID, event_type: "occupancy_session", payload: {} };
    const fetchMock = mockFetchOnce(payload);

    const result = await getOperationalEvent(EVENT_ID);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const url = fetchMock.mock.calls[0]?.[0] as string;
    expect(url).toContain("/operational/events/");
    expect(url).toContain(EVENT_ID);
    // The desktop never sends a tenant or venue — server-side identity only.
    expect(url).not.toContain("tenant");
    expect(url).not.toContain("venue");
    expect(result.event_id).toBe(EVENT_ID);
  });

  it("should fetch the occupancy fact from the authorized endpoint", async () => {
    const payload = { fact_id: FACT_ID, fact_type: "occupancy_snapshot", payload: {} };
    const fetchMock = mockFetchOnce(payload);

    const result = await getOperationalFact(FACT_ID);

    const url = fetchMock.mock.calls[0]?.[0] as string;
    expect(url).toContain("/operational/facts/");
    expect(url).toContain(FACT_ID);
    expect(result.fact_id).toBe(FACT_ID);
  });

  it("should fetch evidence availability from the authorized endpoint", async () => {
    const payload = { event_id: EVENT_ID, available: true, evidence_ref_id: "abc" };
    const fetchMock = mockFetchOnce(payload);

    const result = await getEvidenceAvailability(EVENT_ID);

    const url = fetchMock.mock.calls[0]?.[0] as string;
    expect(url).toContain("/operational/events/");
    expect(url).toContain(EVENT_ID);
    expect(url).toContain("/evidence");
    expect(result.available).toBe(true);
    expect(result.evidence_ref_id).toBe("abc");
  });

  it("should propagate 404 as NOT_FOUND so the card can render its empty state", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "Operational event not found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(getOperationalEvent("missing-event")).rejects.toMatchObject({
      status: 404,
      code: "NOT_FOUND",
    });
  });

  it("should propagate 401 so the card can render its unauthorized state", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "Signature has expired" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(getOperationalEvent(EVENT_ID)).rejects.toMatchObject({
      status: 401,
      code: "UNAUTHENTICATED",
    });
  });
});
