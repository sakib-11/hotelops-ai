/**
 * Vertical-Slice Demo Identities (Task 18.13)
 *
 * The deterministic resource identities of the controlled vertical-slice
 * fixture (tests/unit/fixtures/vertical_slice/manifest.json + the 18.8
 * slice run — the same ids the backend tests seed as authoritative
 * rows). The desktop does NOT compute these (that would be business
 * logic): they are pinned references so the card can fetch the canonical
 * occupancy event + fact through the authorized API surface.
 *
 * These ids are demo/documented values — in production the ids come
 * from the backend (a list/latest endpoint). The card treats them as
 * opaque resource selectors; authorization is enforced server-side.
 */
export const VERTICAL_SLICE_DEMO = {
  eventId: "95947620-fc14-5152-bb13-e373706444f7",
  factId: "376068b5-8c66-589a-ad22-ea586edd14c9",
  venueId: "22222222-2222-4222-8222-222222222222",
  cameraId: "33333333-3333-4333-8333-333333333333",
} as const;
