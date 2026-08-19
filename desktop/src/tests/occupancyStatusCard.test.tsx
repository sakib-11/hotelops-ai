/**
 * Test Suite: OccupancyStatusCard (Task 18.13 — Desktop Vertical Slice)
 *
 * Proves the minimal Tauri card end-to-end through the REAL hooks over
 * the mocked API service boundary (the card must never derive state
 * itself — everything rendered comes from the authorized FastAPI DTOs).
 *
 * States covered (the task's list):
 *   1. loading      — queries pending → loading UI;
 *   2. success      — venue, camera/source, occupancy state/fact,
 *                     event time, event status, evidence availability;
 *   3. empty state  — 404 (event missing/out of scope) → empty UI;
 *   4. unauthorized — 401/403 → unauthorized UI;
 *   5. API failure  — network/5xx → failure UI with retry;
 *   6. stale/error  — cached data past freshness → stale indicator.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  OccupancyStatusCard,
  classifyCardError,
  phaseLabel,
  shortId,
  formatEventTime,
} from "@/features/operational/OccupancyStatusCard";
import { ApiErrorClass, isRetryableError } from "@/api/client/errors";
import type { ApiErrorCode } from "@/api/client/errors";
import type {
  OccupancyEventResponse,
  OccupancyFactResponse,
  EvidenceAvailabilityResponse,
} from "@/api/types/operational";
import * as operationalApi from "@/api/services/operational";

vi.mock("@/api/services/operational", () => ({
  getOperationalEvent: vi.fn(),
  getOperationalFact: vi.fn(),
  getEvidenceAvailability: vi.fn(),
}));

const mockedApi = vi.mocked(operationalApi);

const EVENT_ID = "95947620-fc14-5152-bb13-e373706444f7";
const FACT_ID = "376068b5-8c66-589a-ad22-ea586edd14c9";
const VENUE_ID = "22222222-2222-4222-8222-222222222222";
const CAMERA_ID = "33333333-3333-4333-8333-333333333333";

function makeEvent(): OccupancyEventResponse {
  return {
    event_id: EVENT_ID,
    event_type: "occupancy_session",
    schema_version: "1.0",
    tenant_id: "11111111-1111-4111-8111-111111111111",
    venue_id: VENUE_ID,
    session_id: "cbe7e5c7-486b-5613-bc3d-47d6d6a8205c",
    camera_id: CAMERA_ID,
    event_time: "2026-08-01T10:00:00.800000+00:00",
    produced_at: "2026-08-01T11:00:00+00:00",
    source: "rule:occupancy_session:v1",
    correlation_id: null,
    causation_id: null,
    payload: {
      schema_version: "1.0",
      phase: "started",
      tenant_id: "11111111-1111-4111-8111-111111111111",
      venue_id: VENUE_ID,
      session_id: "cbe7e5c7-486b-5613-bc3d-47d6d6a8205c",
      camera_id: CAMERA_ID,
      spatial_context_id: "zone-lobby",
      occupancy_count: 1,
      occupied_tracks: ["track-person-001"],
      occupancy_time: "2026-08-01T10:00:00.800000+00:00",
      configuration_version_id: "44444444-4444-4444-8444-444444444444",
      rule_id: "occupancy_session",
      rule_version: "v1",
    },
  };
}

function makeFact(): OccupancyFactResponse {
  return {
    fact_id: FACT_ID,
    fact_type: "occupancy_snapshot",
    fsm_kind: "occupancy",
    schema_version: "1.0",
    tenant_id: "11111111-1111-4111-8111-111111111111",
    venue_id: VENUE_ID,
    session_id: "cbe7e5c7-486b-5613-bc3d-47d6d6a8205c",
    camera_id: CAMERA_ID,
    configuration_version_id: "44444444-4444-4444-8444-444444444444",
    event_time: "2026-08-01T10:00:00.800000+00:00",
    source_transition_id: "376068b5-8c66-589a-ad22-ea586edd14c9",
    fsm_version: "1.0",
    policy_revision: "v1",
    payload: {
      schema_version: "1.0",
      snapshot_id: FACT_ID,
      fsm_kind: "occupancy",
      key: {
        fsm_kind: "occupancy",
        tenant_id: "11111111-1111-4111-8111-111111111111",
        venue_id: VENUE_ID,
        session_id: "cbe7e5c7-486b-5613-bc3d-47d6d6a8205c",
        camera_id: CAMERA_ID,
        configuration_version_id: "44444444-4444-4444-8444-444444444444",
        track_id: "track-person-001",
        semantic_context: "zone-lobby",
      },
      event_time: "2026-08-01T10:00:00.800000+00:00",
      previous_count: 0,
      delta: 1,
      occupancy_count: 1,
      occupied_tracks: ["track-person-001"],
      source_transition_id: "376068b5-8c66-589a-ad22-ea586edd14c9",
      fsm_version: "1.0",
      policy_revision: "v1",
    },
  };
}

function makeEvidence(available: boolean): EvidenceAvailabilityResponse {
  return {
    event_id: EVENT_ID,
    available,
    evidence_ref_id: available ? "abc12345-0000-0000-0000-000000000000" : null,
  };
}

function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
}

function renderCard(eventId = EVENT_ID, factId = FACT_ID) {
  const queryClient = createQueryClient();
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <OccupancyStatusCard eventId={eventId} factId={factId} />
    </QueryClientProvider>,
  );
  return { ...utils, queryClient };
}

const apiError = (status: number, code: ApiErrorClass["code"]): ApiErrorClass =>
  new ApiErrorClass({ status, code, message: "err" });

describe("OccupancyStatusCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedApi.getOperationalEvent.mockResolvedValue(makeEvent());
    mockedApi.getOperationalFact.mockResolvedValue(makeFact());
    mockedApi.getEvidenceAvailability.mockResolvedValue(makeEvidence(true));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("1. renders the loading state while queries are pending", () => {
    mockedApi.getOperationalEvent.mockReturnValue(
      new Promise<OccupancyEventResponse>(() => undefined),
    );
    renderCard();
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.getByText("Fetching occupancy event")).toBeInTheDocument();
  });

  it("2. renders the canonical DTO fields on success", async () => {
    renderCard();

    await waitFor(() => expect(screen.getByText("Occupancy started")).toBeInTheDocument());

    // Venue (short id) — the server-provided venue identity.
    expect(screen.getByText(VENUE_ID.slice(0, 8))).toBeInTheDocument();
    // Camera / source.
    expect(screen.getByText(/33333333 · rule:occupancy_session:v1/)).toBeInTheDocument();
    // Occupancy state/fact: phase + count from the event payload.
    expect(screen.getByText(/Occupancy started · 1 person/)).toBeInTheDocument();
    // Event time (server-provided).
    expect(screen.getByText(/2026-08-01 10:00:00 UTC/)).toBeInTheDocument();
    // Event status badge.
    expect(screen.getAllByText("Occupancy started").length).toBeGreaterThanOrEqual(1);
    // Evidence availability — the server-derived answer.
    expect(screen.getByText(/Available · abc12345/)).toBeInTheDocument();
    // Fact row from the canonical fact payload.
    expect(screen.getByText(/count 1 \(prev 0, delta \+1\) · fsm 1.0/)).toBeInTheDocument();
  });

  it("2b. renders 'Not available' evidence when the backend answers false", async () => {
    mockedApi.getEvidenceAvailability.mockResolvedValue(makeEvidence(false));
    renderCard();

    await waitFor(() => expect(screen.getByText(/Not available/)).toBeInTheDocument());
    expect(screen.queryByText(/Available ·/)).not.toBeInTheDocument();
  });

  it("3. renders the empty state when the event is 404 (missing / out of scope)", async () => {
    mockedApi.getOperationalEvent.mockRejectedValue(apiError(404, "NOT_FOUND"));
    renderCard();

    await waitFor(() => expect(screen.getByText("No occupancy event")).toBeInTheDocument());
    expect(screen.queryByText("Occupancy started")).not.toBeInTheDocument();
  });

  it("4. renders the unauthorized state on 401", async () => {
    mockedApi.getOperationalEvent.mockRejectedValue(apiError(401, "UNAUTHENTICATED"));
    renderCard();

    await waitFor(() => expect(screen.getByText("Unauthorized")).toBeInTheDocument());
    expect(screen.getByText(/do not have permission/i)).toBeInTheDocument();
  });

  it("4b. renders the unauthorized state on 403", async () => {
    mockedApi.getOperationalEvent.mockRejectedValue(apiError(403, "FORBIDDEN"));
    renderCard();

    await waitFor(() => expect(screen.getByText("Unauthorized")).toBeInTheDocument());
  });

  it("5. renders the failure state with retry on network failure", async () => {
    mockedApi.getOperationalEvent.mockRejectedValue(
      new ApiErrorClass({
        status: 0,
        code: "NETWORK_ERROR",
        message: "Unable to connect to the server.",
      }),
    );
    renderCard();

    await waitFor(() =>
      expect(screen.getByText("Occupancy status unavailable")).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("5b. renders the failure state on server error (5xx)", async () => {
    mockedApi.getOperationalEvent.mockRejectedValue(apiError(500, "SERVER_ERROR"));
    renderCard();

    await waitFor(() =>
      expect(screen.getByText("Occupancy status unavailable")).toBeInTheDocument(),
    );
  });

  it("6. shows the stale indicator once cached data is past its freshness window", async () => {
    vi.useFakeTimers();
    const { rerender, queryClient } = renderCard();

    // Flush the queries and re-render (no waitFor under fake timers).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText("Occupancy started")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    // Advance past the moderate stale time (2 minutes) and re-render.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(121_000);
    });
    rerender(
      <QueryClientProvider client={queryClient}>
        <OccupancyStatusCard eventId={EVENT_ID} factId={FACT_ID} />
      </QueryClientProvider>,
    );

    expect(screen.getByText("Stale")).toBeInTheDocument();
  });

  it("7. retry after an API failure refetches and recovers", async () => {
    // The event query fails once (500), then succeeds — the card must
    // recover explicitly via its retry action, never silently.
    mockedApi.getOperationalEvent
      .mockRejectedValueOnce(apiError(500, "SERVER_ERROR"))
      .mockResolvedValue(makeEvent());
    renderCard();

    await waitFor(() =>
      expect(screen.getByText("Occupancy status unavailable")).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    await waitFor(() => expect(screen.getByText("Occupancy started")).toBeInTheDocument());
    expect(screen.queryByText("Occupancy status unavailable")).not.toBeInTheDocument();
    // The recovered card renders the canonical DTO again.
    expect(screen.getByText(/Available · abc12345/)).toBeInTheDocument();
  });

  it("8. classifies EVERY API error code into an explicit card state", () => {
    // No error is ever silent: every normalized code maps to a visible
    // card state (unauthorized / empty / failure).
    const statusFor = (code: ApiErrorCode): number =>
      code === "UNAUTHENTICATED"
        ? 401
        : code === "FORBIDDEN"
          ? 403
          : code === "NOT_FOUND"
            ? 404
            : code === "NETWORK_ERROR" || code === "TIMEOUT"
              ? 0
              : 500;
    const codes: ApiErrorCode[] = [
      "UNAUTHENTICATED",
      "FORBIDDEN",
      "NOT_FOUND",
      "CONFLICT",
      "VALIDATION_ERROR",
      "RATE_LIMITED",
      "SERVER_ERROR",
      "SERVICE_UNAVAILABLE",
      "NETWORK_ERROR",
      "TIMEOUT",
      "MALFORMED_RESPONSE",
      "UNKNOWN",
    ];
    for (const code of codes) {
      const state = classifyCardError(apiError(statusFor(code), code));
      expect(state).toBeDefined();
      expect(["unauthorized", "empty", "failure"]).toContain(state);
    }
    // 404 is empty, 401/403 are unauthorized, everything else is failure.
    expect(classifyCardError(apiError(404, "NOT_FOUND"))).toBe("empty");
    expect(classifyCardError(apiError(401, "UNAUTHENTICATED"))).toBe("unauthorized");
    expect(classifyCardError(apiError(403, "FORBIDDEN"))).toBe("unauthorized");
  });

  it("9. the retry policy classifies retryable vs terminal API failures", () => {
    // Correct retry behavior: only transient classes retry; authorization
    // and data-condition errors never retry silently.
    const retryable: ApiErrorCode[] = [
      "NETWORK_ERROR",
      "TIMEOUT",
      "SERVER_ERROR",
      "SERVICE_UNAVAILABLE",
      "RATE_LIMITED",
    ];
    for (const code of retryable) {
      expect(isRetryableError(apiError(500, code))).toBe(true);
    }
    const terminal: ApiErrorCode[] = [
      "UNAUTHENTICATED",
      "FORBIDDEN",
      "NOT_FOUND",
      "CONFLICT",
      "VALIDATION_ERROR",
      "MALFORMED_RESPONSE",
      "UNKNOWN",
    ];
    for (const code of terminal) {
      expect(isRetryableError(apiError(404, code))).toBe(false);
    }
  });
});

describe("OccupancyStatusCard presentational helpers", () => {
  it("classifies errors into card states", () => {
    expect(classifyCardError(null)).toBeNull();
    expect(classifyCardError(apiError(404, "NOT_FOUND"))).toBe("empty");
    expect(classifyCardError(apiError(401, "UNAUTHENTICATED"))).toBe("unauthorized");
    expect(classifyCardError(apiError(403, "FORBIDDEN"))).toBe("unauthorized");
    expect(classifyCardError(apiError(500, "SERVER_ERROR"))).toBe("failure");
    expect(classifyCardError(new Error("boom"))).toBe("failure");
  });

  it("maps occupancy phases to labels (presentation only)", () => {
    expect(phaseLabel("started")).toBe("Occupancy started");
    expect(phaseLabel("ended")).toBe("Occupancy ended");
  });

  it("shortens and formats identities/times without computation", () => {
    expect(shortId(VENUE_ID)).toBe(VENUE_ID.slice(0, 8));
    expect(shortId(null)).toBe("—");
    expect(formatEventTime("2026-08-01T10:00:00.800000+00:00")).toBe("2026-08-01 10:00:00 UTC");
    expect(formatEventTime("garbage")).toBe("—");
  });
});
