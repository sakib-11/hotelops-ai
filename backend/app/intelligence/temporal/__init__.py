"""Deterministic temporal intelligence foundation (Task 15 Step 1).

Converts canonical Task 14 observations into stable, checkpointable
time-based state under an explicit event-time discipline — pure,
deterministic, versioned, and isolated per ``TemporalStateKey``.

- ``exceptions`` — the typed error taxonomy (``TemporalError`` and
  subclasses); malformed input is never repaired.
- ``fsm`` — the reusable deterministic FSM abstraction
  (``DeterministicFsm`` + ``FsmRule``), the same legal-transitions
  convention the configuration state machine uses, made generic.
- ``presence`` — the canonical enter/exit FSM (Task 15.2: ABSENT /
  ENTERING / PRESENT / EXITING) and the structural
  ``presence_kind(SpatialObservation)`` mapping.
- ``dwell`` — the Task 15.3 dwell FSM (``DWELL_FSM`` + ``DwellEngine``)
  deriving ``DwellInterval`` facts from the presence FSM's confirmed
  transitions via ``dwell_event_from_presence``.
- ``occupancy`` — the Task 15.4 occupancy aggregator
  (``OCCUPANCY_FSM`` + ``OccupancyEngine``) deriving per-scope
  ``OccupancySnapshot`` facts from confirmed presence via
  ``occupancy_event_from_presence``; ``occupancy_scope_key`` derives the
  canonical aggregate scope key from a presence key.
- ``movement`` — the Task 15.5.1 movement foundation
  (``MOVEMENT_FSM`` + ``MovementEngine``) classifying a tracked entity
  as UNKNOWN / STATIONARY / MOVING from consecutive canonical spatial
  observations, emitting ``MovementMeasurement`` facts.
- ``classification`` — the Task 15.5.2 movement classification
  (``MOVEMENT_CLASSIFICATION_FSM`` + ``MovementClassificationEngine``)
  consuming those measurements and applying hysteresis + event-time
  qualification, emitting ``MovementClassificationTransition`` facts;
  ``classification_input_from_movement`` is the sanctioned wiring.
- ``waiting`` — the Task 15.5.3 waiting detection
  (``WAITING_FSM`` + ``WaitingEngine``) interpreting a confirmed-PRESENT,
  stationary entity inside an explicitly configured waiting-capable
  context into ``WaitingState``/``WaitingInterval`` facts;
  ``waiting_event_from_presence`` is the sanctioned presence wiring and
  ``waiting_contexts_from_configuration`` derives the explicit waiting
  context set from a Task 10 configuration snapshot.
- ``engine`` — the pure engine: ``TemporalEngine`` (event-time ordering,
  watermark, late/out-of-order policy, idempotent dedup, key/provenance
  integrity, checkpoint/restore) and ``PresenceTemporalEngine``
  (hysteresis/occlusion/grace semantics from ``TemporalPolicy``).

No I/O, no current-time reads, no fallback to \"the latest configuration\"
— persistence is a separate adapter boundary over serializable
checkpoints.
"""

from backend.app.intelligence.temporal.classification import (
    MOVEMENT_CLASSIFICATION_FSM,
    MovementClassificationEngine,
    MovementClassificationInput,
    MovementClassificationResult,
    classification_input_from_movement,
)
from backend.app.intelligence.temporal.dwell import (
    DWELL_FSM,
    DwellEngine,
    dwell_event_from_presence,
)
from backend.app.intelligence.temporal.engine import (
    Evaluation,
    PresenceTemporalEngine,
    TemporalEngine,
    TemporalInput,
    TemporalResult,
)
from backend.app.intelligence.temporal.exceptions import (
    CheckpointIntegrityError,
    FsmVersionMismatchError,
    InvalidTemporalInputError,
    InvalidTransitionError,
    LateEventError,
    StateKeyMismatchError,
    TemporalError,
)
from backend.app.intelligence.temporal.fsm import (
    DeterministicFsm,
    FsmRule,
)
from backend.app.intelligence.temporal.movement import (
    MOVEMENT_FSM,
    MovementEngine,
    MovementInput,
    MovementResult,
)
from backend.app.intelligence.temporal.occupancy import (
    OCCUPANCY_FSM,
    OCCUPANCY_SCOPE_TRACK,
    OccupancyEngine,
    OccupancyInput,
    OccupancyResult,
    occupancy_event_from_presence,
    occupancy_scope_key,
)
from backend.app.intelligence.temporal.presence import (
    PRESENCE_FSM,
    presence_kind,
)
from backend.app.intelligence.temporal.waiting import (
    WAITING_FSM,
    WaitingEngine,
    WaitingInput,
    WaitingResult,
    waiting_contexts_from_configuration,
    waiting_event_from_presence,
)

__all__ = [
    "DWELL_FSM",
    "MOVEMENT_CLASSIFICATION_FSM",
    "MOVEMENT_FSM",
    "OCCUPANCY_FSM",
    "OCCUPANCY_SCOPE_TRACK",
    "PRESENCE_FSM",
    "WAITING_FSM",
    "CheckpointIntegrityError",
    "DeterministicFsm",
    "DwellEngine",
    "Evaluation",
    "FsmRule",
    "FsmVersionMismatchError",
    "InvalidTemporalInputError",
    "InvalidTransitionError",
    "LateEventError",
    "MovementClassificationEngine",
    "MovementClassificationInput",
    "MovementClassificationResult",
    "MovementEngine",
    "MovementInput",
    "MovementResult",
    "OccupancyEngine",
    "OccupancyInput",
    "OccupancyResult",
    "PresenceTemporalEngine",
    "StateKeyMismatchError",
    "TemporalEngine",
    "TemporalError",
    "TemporalInput",
    "TemporalResult",
    "WaitingEngine",
    "WaitingInput",
    "WaitingResult",
    "classification_input_from_movement",
    "dwell_event_from_presence",
    "occupancy_event_from_presence",
    "occupancy_scope_key",
    "presence_kind",
    "waiting_contexts_from_configuration",
    "waiting_event_from_presence",
]
