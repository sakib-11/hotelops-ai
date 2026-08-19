/**
 * OccupancyStatusCard - Minimal vertical-slice card (Task 18.13).
 *
 * Proves the complete slice in the Tauri desktop WITHOUT any business
 * logic: the card only READS what the authorized FastAPI retrieval
 * surface returns and renders it.
 *
 *   Tauri → API client → FastAPI → authorized repository → PostgreSQL
 *
 * It does NOT run YOLO/ByteTrack, does NOT calculate occupancy, does NOT
 * touch PostgreSQL directly, and never derives evidence availability —
 * every value on the card is a canonical DTO field from the backend
 * (contracts/operational, mirrored in src/api/types/operational.ts).
 *
 * Rendered states:
 *   loading      — the event/fact queries are pending;
 *   success      — the canonical DTOs are rendered as-is;
 *   empty        — the event does not exist / is out of scope (404);
 *   unauthorized — the actor cannot read this resource (401/403);
 *   failure      — network / server failure (retryable);
 *   stale/error  — the cached data is stale (refetch indicator).
 */

import { useQueryClient } from "@tanstack/react-query";
import { useOperationalEvent, useOperationalFact, useEvidenceAvailability } from "@/query/hooks";
import { queryKeys } from "@/query/keys";
import { ApiErrorClass, isAuthenticationError } from "@/api/client/errors";
import {
  Badge,
  Card,
  CardContent,
  CardHeader,
  EmptyState,
  ErrorState,
  LoadingState,
  StatusBadge,
} from "@/components/ui";
import type { OccupancySessionPhase } from "@/api/types/operational";
import "./OccupancyStatusCard.css";

export interface OccupancyStatusCardProps {
  /** The vertical-slice occupancy event identity (Task 16). */
  eventId: string;
  /** The vertical-slice occupancy fact identity (Task 15). */
  factId: string;
}

/** Which high-level state the card is in — driven by the event query. */
export type OccupancyCardState = "loading" | "success" | "empty" | "unauthorized" | "failure";

/** Classify a query error into a card state (401/403 vs 404 vs other). */
export function classifyCardError(
  error: Error | null,
): Exclude<OccupancyCardState, "loading" | "success"> | null {
  if (!error) return null;
  if (
    isAuthenticationError(error) ||
    (error instanceof ApiErrorClass && error.code === "FORBIDDEN")
  ) {
    return "unauthorized";
  }
  if (error instanceof ApiErrorClass && error.code === "NOT_FOUND") {
    return "empty";
  }
  return "failure";
}

/** Presentational mapping — never business logic. */
export function phaseLabel(phase: OccupancySessionPhase): string {
  return phase === "started" ? "Occupancy started" : "Occupancy ended";
}

/** Short display form of a UUID-ish identity (first 8 chars). */
export function shortId(value: string | null | undefined): string {
  return value ? value.slice(0, 8) : "—";
}

/** Deterministic UTC display of an event/fact time (seconds precision). */
export function formatEventTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  // toISOString(): "2026-08-01T10:00:00.800Z" → "2026-08-01 10:00:00 UTC"
  return date
    .toISOString()
    .replace("T", " ")
    .replace(/\.\d{3}Z$/, " UTC")
    .replace("Z", " UTC");
}

export function OccupancyStatusCard({ eventId, factId }: OccupancyStatusCardProps) {
  const queryClient = useQueryClient();

  const eventQuery = useOperationalEvent(eventId);
  const factQuery = useOperationalFact(factId);
  const evidenceQuery = useEvidenceAvailability(eventId);

  const refetchAll = (): void => {
    void queryClient.refetchQueries({ queryKey: queryKeys.operational.all() });
  };

  // ---- Primary state: the event is the card's canonical resource ----
  if (eventQuery.isPending) {
    return (
      <Card className="occupancy-card" data-testid="occupancy-card">
        <CardHeader title="Occupancy Status" description="Loading the vertical slice…" />
        <CardContent>
          <LoadingState size="md" label="Fetching occupancy event" />
        </CardContent>
      </Card>
    );
  }

  const errorState = classifyCardError(eventQuery.error ?? null);

  if (errorState === "unauthorized") {
    return (
      <Card className="occupancy-card" data-testid="occupancy-card">
        <CardHeader title="Occupancy Status" description="Authorization required" />
        <CardContent>
          <ErrorState
            title="Unauthorized"
            message="You do not have permission to view this occupancy event. Sign in with an authorized account."
            onRetry={refetchAll}
          />
        </CardContent>
      </Card>
    );
  }

  if (errorState === "empty") {
    return (
      <Card className="occupancy-card" data-testid="occupancy-card">
        <CardHeader title="Occupancy Status" description="No event found" />
        <CardContent>
          <EmptyState
            title="No occupancy event"
            description="This occupancy event does not exist or is outside your venue access."
          />
        </CardContent>
      </Card>
    );
  }

  if (errorState === "failure") {
    return (
      <Card className="occupancy-card" data-testid="occupancy-card">
        <CardHeader title="Occupancy Status" description="Unable to load" />
        <CardContent>
          <ErrorState
            title="Occupancy status unavailable"
            message="The service could not be reached. Check your connection and try again."
            onRetry={refetchAll}
          />
        </CardContent>
      </Card>
    );
  }

  // The success branch is only reachable with data (loading/error handled
  // above) — narrow it explicitly so no non-null assertion is needed.
  const event = eventQuery.data;
  if (!event) {
    return null;
  }
  const fact = factQuery.data;
  const evidence = evidenceQuery.data;

  // Stale indicator: data present but past its freshness window (or a
  // background refetch failed) — never blocks the rendered data.
  const isStale = eventQuery.isStale || eventQuery.isRefetchError || factQuery.isStale;

  return (
    <Card className="occupancy-card" data-testid="occupancy-card">
      <CardHeader
        title="Occupancy Status"
        description={event.source}
        action={
          isStale ? (
            <StatusBadge status="stale" label="Stale" />
          ) : (
            <Badge variant="secondary" size="sm" dot>
              Live
            </Badge>
          )
        }
      />
      <CardContent>
        <dl className="occupancy-card__grid">
          <div className="occupancy-card__field">
            <dt>Venue</dt>
            <dd title={event.venue_id}>{shortId(event.venue_id)}</dd>
          </div>
          <div className="occupancy-card__field">
            <dt>Camera / Source</dt>
            <dd title={event.camera_id ?? undefined}>
              {shortId(event.camera_id)} · {event.source}
            </dd>
          </div>
          <div className="occupancy-card__field">
            <dt>Occupancy state</dt>
            <dd>
              {phaseLabel(event.payload.phase)} · {event.payload.occupancy_count} person
              {event.payload.occupancy_count === 1 ? "" : "s"}
            </dd>
          </div>
          <div className="occupancy-card__field">
            <dt>Event time</dt>
            <dd>{formatEventTime(event.event_time)}</dd>
          </div>
          <div className="occupancy-card__field">
            <dt>Event status</dt>
            <dd>
              <StatusBadge
                status={event.payload.phase === "started" ? "processing" : "completed"}
                label={phaseLabel(event.payload.phase)}
                showDot
              />
            </dd>
          </div>
          <div className="occupancy-card__field">
            <dt>Evidence</dt>
            <dd>
              {evidence ? (
                evidence.available ? (
                  <Badge variant="success" size="sm" dot>
                    Available
                    {evidence.evidence_ref_id ? ` · ${shortId(evidence.evidence_ref_id)}` : ""}
                  </Badge>
                ) : (
                  <Badge variant="secondary" size="sm">
                    Not available
                  </Badge>
                )
              ) : (
                <span className="occupancy-card__muted">Unavailable</span>
              )}
            </dd>
          </div>
          {fact && (
            <div className="occupancy-card__field occupancy-card__field--span">
              <dt>Fact</dt>
              <dd>
                count {fact.payload.occupancy_count} (prev {fact.payload.previous_count}, delta{" "}
                {fact.payload.delta > 0 ? "+" : ""}
                {fact.payload.delta}) · fsm {fact.payload.fsm_version}
              </dd>
            </div>
          )}
        </dl>
      </CardContent>
    </Card>
  );
}
