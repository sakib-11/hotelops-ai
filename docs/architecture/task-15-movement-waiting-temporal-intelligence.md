# Task 15.5 — Movement & Waiting Temporal Intelligence

**Status: Implemented & tested (see the acceptance matrix below).**

Task 15.5 converts canonical Task 14 spatial observations into stable,
deterministic movement and waiting intelligence. It is a pure-domain
layer: the engines receive canonical `SpatialObservation`s and return
canonical temporal facts (`MovementMeasurement`, `MovementClassificationTransition`,
`WaitingInterval`). No PostgreSQL, Redis, S3, HTTP, FastAPI, or LLM calls
exist in the core, and no current time is ever read — event-time is the
single source of temporal meaning.

The layer is built in three steps, each consuming the previous one
without duplicating its logic:

```
TrackObservation
    ↓ Task 14 point policy (extract_point) — canonical position
SpatialObservation (canonical point + event_time + provenance)
    ↓ 15.5.1 MovementEngine — pair math, ordering, dedup, reorder
MovementMeasurement (distance + time_delta + provenance)
    ↓ classification_input_from_movement (the only sanctioned wiring)
15.5.2 MovementClassificationEngine — hysteresis + event-time qualification
MovementClassificationState + MovementClassificationTransition
    ↓ presence FSM (15.2) confirmed transitions + classification state
15.5.3 WaitingEngine — waiting context + qualification
WaitingInterval (fact)
```

---

## 1. What was built

| Component | Files | Purpose |
|-----------|-------|---------|
| Movement foundation (15.5.1) | `backend/app/intelligence/temporal/movement.py` | Classifies a tracked entity as UNKNOWN / STATIONARY / MOVING from consecutive canonical observations; emits `MovementMeasurement` facts (distance + time delta of a pair) |
| Movement classification (15.5.2) | `backend/app/intelligence/temporal/classification.py` | Consumes measurements verbatim (never recomputing distance) and applies hysteresis + event-time qualification; emits `MovementClassificationTransition` facts |
| Waiting detection (15.5.3) | `backend/app/intelligence/temporal/waiting.py` | Interprets a confirmed-PRESENT, stationary entity inside an explicitly configured waiting-capable context into NOT_WAITING / WAITING_CANDIDATE / WAITING; emits `WaitingInterval` facts |
| FSM abstraction | `backend/app/intelligence/temporal/fsm.py` | The reusable deterministic FSM (`DeterministicFsm` + `FsmRule`) shared by every family |
| Error taxonomy | `backend/app/intelligence/temporal/exceptions.py` | `TemporalError` base + typed subclasses; malformed input is never repaired |
| Temporal contracts | `contracts/temporal/models.py` | `MovementState`, `MovementMeasurement`, `MovementCheckpoint`, `MovementClassificationState`, `MovementClassificationTransition`, `MovementClassificationCheckpoint`, `WaitingState`, `WaitingInterval`, `WaitingCheckpoint`, `TemporalPolicy`, `TemporalStateKey` |
| Public exports | `backend/app/intelligence/temporal/__init__.py` | The sanctioned imports (`waiting_event_from_presence`, `classification_input_from_movement`, etc.) |

---

## 2. Movement measurement (15.5.1 §3)

For two consecutive in-order observations of the same entity:

```
distance   = Euclidean(previous_position, current_position)
time_delta = current_event_time - previous_event_time
```

- The canonical `SpatialPointModel` is reused verbatim — no new coordinate
  policy. The pair's declared `coordinate_space` is preserved, and the
  engine **refuses to measure a pair whose two points live in different
  spaces** (mixing IMAGE_NORMALIZED and VENUE_LOCAL displacement is
  undefined).
- Distance is a displacement in the declared space ONLY. The engine
  deliberately computes **no velocity**: an image-normalized displacement
  is never pretended to be physical speed (no conversion policy exists,
  so none is invented).
- Every measurement preserves provenance via the nested `TemporalStateKey`
  (tenant / venue / session / camera / configuration version / track /
  spatial context) — never duplicated.
- `time_delta_seconds` may be 0.0 for equal event timestamps (handled,
  never a division).

## 3. Movement state (15.5.1 §6 / 15.5.2)

The only movement states are `MOVEMENT_STATES = ("unknown", "stationary", "moving")`,
declared once and shared by the contract validator and every FSM — the
two can never drift apart. No waiting / queueing / service / session
states exist in this family.

- **UNKNOWN** — the pristine per-track state; the first observation of a
  track is the measurement anchor (a measurement is a PAIR, so no
  measurement is emitted yet).
- **STATIONARY** — a pair whose distance is at-or-below the configured
  stationary policy.
- **MOVING** — a pair whose distance strictly exceeds the configured
  movement policy.

## 4. Configuration (Part 6)

All thresholds are configuration-driven through `TemporalPolicy`
(never hardcoded):

| Field | Semantics |
|-------|-----------|
| `movement_threshold` | 15.5.1 single-threshold knob (strictly-above = MOVING) |
| `movement_enter_threshold` | 15.5.2 displacement above which a measurement is MOVING evidence (strictly-above) |
| `movement_exit_threshold` | 15.5.2 displacement below which a measurement is STATIONARY evidence (strictly-below) |
| `movement_qualification_seconds` | event-time window evidence must stay sustained before the classification changes |
| `waiting_qualification_seconds` | event-time duration a WAITING_CANDIDATE must persist before WAITING |
| `waiting_contexts` | the EXPLICIT set of waiting-capable `semantic_context` values |
| `revision` | identifies the exact configuration; checkpoints restored under a different revision are rejected |

**Validation:** `movement_exit_threshold <= movement_enter_threshold` is
validated at `TemporalPolicy` construction. Invalid configuration fails
explicitly — never silently swapped or repaired.

**Versioning / provenance (Part 21):** the key carries the pinned
`configuration_version_id` and every fact carries `policy_revision`. A
session governed by configuration V1 keeps using V1 even after V2 is
published; replay never queries "the latest configuration".

## 5. Hysteresis (15.5.2 / Task 15.5 §7)

A measurement in the hysteresis band (`exit < distance < enter`) retains
the current classification in BOTH directions — the exact anti-flap guard
for small positional noise near a boundary:

```
Current = MOVING     + band measurement  → MOVING
Current = STATIONARY + band measurement  → STATIONARY
```

The enter threshold dominates the exit threshold; equal thresholds are the
degenerate single-boundary case. A band measurement also cancels any
in-progress qualification run — ambiguous evidence is never "qualified".

## 6. Temporal qualification (15.5.2 / Task 15.5 §8–§9)

The classification only changes once the evidence stays in the direction
away from the current state for `movement_qualification_seconds` of EVENT
time. Candidates are represented as pending-transition metadata
(`pending_state` + `qualification_started` on the state) — not extra FSM
states, reusing the established pattern:

```
STATIONARY → (above-enter evidence) → pending MOVING → sustained → MOVING
MOVING     → (below-exit evidence)  → pending STATIONARY → sustained → STATIONARY
```

- The transition's `event_time` is the confirming measurement's event
  time (the qualification-completed boundary) — never the run start,
  never processing time.
- `0.0` disables qualification: one qualifying measurement changes the
  state immediately (the degenerate single-threshold behavior).
- **Candidate cancellation (§9):** a contradicting measurement or a band
  measurement cancels the run deterministically — no stale candidate
  survives, no MOVING/STATIONARY transition is emitted.

## 7. Waiting semantics (15.5.3 / Task 15.5 §10–§16)

**WAITING is NOT STATIONARY.** STATIONARY is the 15.5.2 movement
classification; WAITING is an operational interpretation that requires
ALL of:

1. confirmed PRESENT (per the 15.2 presence FSM),
2. a configured waiting-capable spatial context (`TemporalPolicy.waiting_contexts`),
3. a STATIONARY 15.5.2 classification,
4. the configured waiting qualification duration of event time,
5. valid event-time timestamps,
6. valid spatial/configuration provenance.

State model (minimal — no extra states):

```
not_waiting --candidate_started--> waiting_candidate
waiting_candidate --waiting_confirmed--> waiting
waiting_candidate --candidate_aborted--> not_waiting
waiting --waiting_ended--> not_waiting
* --stay--> same state
```

- **Context (Part 12):** waiting is restricted to explicitly configured
  contexts — queue areas, service areas, and `ZoneType.WAITING_AREA`
  zones only, derived with `waiting_contexts_from_configuration`. An
  empty set disables waiting everywhere. lobby / hallway / entrance /
  restaurant / table are NEVER waiting contexts by themselves.
- **Qualification (Part 13):** `candidate_start` is the event-time the
  entity first satisfied (present + stationary + context); the confirmed
  `waiting_start` is the event-time the qualification completed — the
  two are kept distinct, and neither is processing time.
- **Cancellation (Part 14):** MOVING confirmation (15.5.2), confirmed
  presence loss (exit / occlusion expiry / session closure) before
  qualification → NOT_WAITING with no waiting fact.
- **Continuation (Part 15):** while WAITING, an in-order present +
  stationary + context observation is a `stay` — no new fact per frame.
- **Termination (Part 16):** confirmed exit, occlusion expiry, session
  closure, or a 15.5.2 MOVING confirmation closes the interval with the
  correct reason. Task 15.6 session closure logic is NOT implemented here.
- **Re-entry (Part 25 E):** each confirmed entry after a confirmed exit
  opens a NEW candidate and NEW interval; intervals are never merged.
  The interval id binds waiting_start (+ waiting_end when closed), so
  two intervals of the same track/context are distinct facts.

## 8. Event-time, late / out-of-order, duplicates (Parts 4 / 19 / 28–29)

The 15.1 policy is reused verbatim — no second ordering mechanism:

- `event_time` is authoritative; ordering uses `(event_time, frame_id)`;
  processing time is metadata only and never determines temporal meaning.
- A duplicate of the last applied position → DEDUPLICATED (no
  measurement, no state change, no duplicate transition or interval).
- A position older than the watermark within `reorder_window_seconds` →
  REORDERED: accepted with no rewind (classification, qualification run,
  candidate, and watermark are preserved); older → `LateEventError`.
- Equal event timestamps with later frame ids are applied (a valid pair
  with `time_delta == 0`).
- Transition/measurement/interval IDs are content-derived (UUID5 over the
  canonical key + inputs + outcome), so replaying the same timeline
  reproduces byte-identical output — the Task 7 idempotency principle,
  reused rather than re-architected.

## 9. Occlusion (Part 18)

The 15.1/15.2 grace policy is reused verbatim — no second occlusion
mechanism. A short `not_observed` gap keeps the presence FSM PRESENT
(TEMPORARILY_MISSING → `stay` at the waiting layer), so movement state,
stationary qualification, waiting candidates, and confirmed waiting are
all preserved. Only a confirmed `missing_expired` (the gap exceeded the
configured occlusion tolerance) ends them.

## 10. Spatial boundary stability (Part 17)

The waiting context capability is pinned per `TemporalStateKey` (the
`semantic_context` is part of the key and the policy is pinned to the
session). Context transitions are expressed exclusively through the
presence FSM's confirmed exit/entry — boundary noise that Task 14 does
not confirm as a context change never flips the waiting zone.

## 11. Isolation (Part 20)

Every `TemporalStateKey` is an independent per-track state. The engines
verify tenant / venue / session / camera / configuration version / track /
context agreement between the key and every input (observation,
measurement, transition, classification state), raising
`StateKeyMismatchError` otherwise. Cross-tenant / cross-venue /
cross-session / cross-track state is impossible — the same track id in
two sessions has two keys and never shares state.

## 12. Checkpoint / restart recovery (Parts 22–23)

Each family exposes a versioned checkpoint (`MovementCheckpoint`,
`MovementClassificationCheckpoint`, `WaitingCheckpoint`) carrying the
engine version + policy revision + the minimal state needed to resume:

- movement: current classification, previous position, previous event
  time, watermark, key (configuration version inside);
- classification: classification, `state_since`, pending run
  (`pending_state` + `qualification_started`), watermark, key;
- waiting: classification, `presence_confirmed`, `candidate_start`,
  `waiting_start`, watermark, key.

Restoring under a different engine version, FSM version, or policy
revision is rejected with a typed error — historical state is never
silently reinterpreted. Checkpoints hold scalars only: no unbounded
observation history is stored. Restart recovery (process → checkpoint →
stop → restore → continue) produces byte-identical results to
uninterrupted processing, for stationary qualification, movement
qualification, and waiting qualification.

## 13. Failure behavior (Part 30)

All failures are deterministic and typed (never fabricated state):

| Error | Meaning |
|-------|---------|
| `InvalidTemporalInputError` | missing/wrong-typed inputs, naive timestamps, invalid movement state values, mis-wired observation kinds |
| `LateEventError` | event_time older than the reordering window allows |
| `InvalidTransitionError` | the FSM has no legal transition for the requested step |
| `StateKeyMismatchError` | key provenance disagrees with the input (cross-scope) |
| `FsmVersionMismatchError` | checkpoint version drift |
| `CheckpointIntegrityError` | structurally invalid checkpoint or policy revision drift |

Configuration is validated at construction (`exit <= enter`, non-negative
durations); malformed configuration is never swapped or repaired.

## 14. Performance / memory (Part 31)

Every state holds scalars only (the measurement anchor, the pending run,
the candidate/waiting start, the watermark). Each step needs exactly the
current observation/measurement — there is no per-frame history, no
unbounded retention, no O(n²) processing. The engines perform no database
or network calls; checkpoint size is bounded by construction.

## 15. Observability (Part 32)

The core emits canonical facts carrying the full structured context
(tenant / venue / session / camera / track / spatial context via the key,
event_time, configuration_version_id, policy_revision, fsm_version, and
transition/candidate/interval identity). Structured logging and tracing
of these facts happen at the transport/adapter layer (Task 8
infrastructure) — the pure core performs no logging of its own, no
`print()`, and carries no secrets.

## 16. Test coverage

| Suite | Covers |
|-------|--------|
| `tests/unit/test_movement_foundation.py` | 15.5.1: UNKNOWN anchor, STATIONARY/MOVING, measurement provenance, equal timestamps, dedup/reorder, isolation, checkpoint |
| `tests/unit/test_movement_classification.py` | 15.5.2: hysteresis band, qualification, candidate lifecycle, event-time authority, isolation |
| `tests/unit/test_waiting_fsm.py` | 15.5.3: golden valid/false waiting, stationary non-waiting, moving-through-zone, re-entry, cancellation, continuation, termination, occlusion, checkpoint, failure tests |
| `tests/unit/test_hysteresis_qualification.py` | hysteresis + qualification hardening, jitter around thresholds, event-time/late/out-of-order, duplicates, isolation, provenance, restart recovery |
| `tests/unit/test_temporal_foundation.py` | 15.1 foundation discipline the engines reuse (ordering, watermark, late policy, checkpoint) |

## Acceptance matrix

| Requirement | Status |
|-------------|--------|
| Movement measurement (distance + time delta, provenance) | ✅ `MovementEngine._build_measurement` |
| UNKNOWN / STATIONARY / MOVING | ✅ `MOVEMENT_STATES` (single shared declaration) |
| Enter/exit thresholds, config-driven | ✅ `TemporalPolicy.movement_enter_threshold` / `movement_exit_threshold` |
| Threshold validation (`exit <= enter`) | ✅ `TemporalPolicy._validate_hysteresis` |
| Hysteresis band retains state | ✅ `MovementClassificationEngine._classify` (`evidence == "band"`) |
| Movement temporal qualification | ✅ `pending_state` + `qualification_started`, event-time |
| Candidate cancellation | ✅ contradicting/band measurement cancels the run |
| Waiting context (explicit config only) | ✅ `waiting_contexts_from_configuration` + `TemporalPolicy.waiting_contexts` |
| Waiting candidate / qualification / confirmation | ✅ `WAITING_FSM` + `WaitingEngine._evaluate` |
| Waiting cancellation / continuation / termination | ✅ candidate_aborted / stay / waiting_ended |
| Stationary ≠ waiting; moving-through-zone ≠ waiting | ✅ golden tests |
| Event-time authoritative; no processing-time semantics | ✅ watermark ordering; `processing_time` is metadata only |
| Late / out-of-order policy reused | ✅ 15.1 `reorder_window_seconds` + `LateEventError` |
| Duplicates idempotent (no double transition/interval) | ✅ content-derived IDs + watermark dedup |
| Tenant / venue / session / camera / track / context isolation | ✅ key provenance + `StateKeyMismatchError` |
| Configuration provenance + historical replay | ✅ pinned `configuration_version_id` + `policy_revision` |
| Checkpoint + restart recovery | ✅ `MovementCheckpoint` / `MovementClassificationCheckpoint` / `WaitingCheckpoint` |
| Jitter / golden movement / golden waiting / failure tests | ✅ unit suites above |
| Bounded state, no I/O in core | ✅ scalars only; purity tests |
| pytest / Ruff format / Ruff lint / mypy | ✅ green |

## Related documentation

- [ADR-010 — Geometry Model & Spatial Semantics](adr/adr-010-geometry-model-spatial-semantics.md) — the canonical point policy and coordinate semantics
- [Task 7 — Outbox, Inbox & Idempotency](task-7-outbox-inbox-idempotency.md) — the idempotency principle the content-derived IDs reuse
- [ADR-002 — Deterministic Core with LLM Last](adr/ADR-002-deterministic-core-llm-last.md) — the deterministic-core boundary this task obeys
