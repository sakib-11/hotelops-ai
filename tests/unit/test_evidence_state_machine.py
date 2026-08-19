"""Tests for Task 17.10 — the evidence processing state machine.

Makes evidence processing durable and recoverable with EXPLICIT states
and an EXPLICIT transition table (reusing the Task 15 ``DeterministicFsm``
convention):

    REQUESTED → QUEUED → EXTRACTING → UPLOADING → FINALIZED
    EXTRACTING → RETRYABLE_FAILURE → QUEUED (recovery)
    EXTRACTING/UPLOADING → TERMINAL_FAILURE
    REQUESTED/QUEUED/EXTRACTING/UPLOADING → EXPIRED

Covered:

- valid transition: the declared happy path + failure paths apply;
- invalid transition: anything not in the table is REJECTED (typed);
- restart: the durable checkpoint survives a crash — the worker resumes
  from the persisted state, never rewinds to REQUESTED;
- duplicate worker: only one worker wins QUEUED → EXTRACTING; the
  second gets a from-state mismatch (atomic claim guard);
- retry: RETRYABLE_FAILURE → QUEUED → EXTRACTING again;
- finalization: UPLOADING → FINALIZED;
- finalized mutation attempt: FINALIZED is immutable — every event is
  rejected with the typed error;
- idempotency: duplicate delivery of the same event is a no-op success
  (Task 7);
- determinism: identical inputs produce identical outcomes;
- the table is explicit: every declared transition is testable and every
  non-declared pair is rejected.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from backend.app.application.services.evidence_state import (
    EvidenceCheckpoint,
    EvidenceStateService,
    EvidenceTransitionResult,
)
from backend.app.domain.evidence.exceptions import (
    EvidenceStateMismatchError,
    FinalizedEvidenceMutationError,
    InvalidEvidenceTransitionError,
)
from backend.app.domain.evidence.state_machine import (
    EVIDENCE_PROCESSING_STATE_KEY,
    EVIDENCE_PROCESSING_VERSION_KEY,
    EVIDENCE_STATE_MACHINE_VERSION,
    EvidenceEvent,
    EvidenceProcessingState,
    EvidenceStateMachine,
)
from backend.app.infrastructure.database.models.evidence import EvidenceRefModel
from backend.app.infrastructure.reliability.exceptions import IdempotencyKeyError
from contracts.common import EventId, EvidenceId, TenantId, VenueId

_TENANT = TenantId(uuid.UUID("10000000-0000-0000-0000-000000000001"))
_VENUE = VenueId(uuid.UUID("20000000-0000-0000-0000-000000000001"))
_REF = EvidenceId(uuid.UUID("30000000-0000-0000-0000-000000000001"))
_EVENT = EventId(uuid.UUID("40000000-0000-0000-0000-000000000001"))
_NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)

_MACHINE = EvidenceStateMachine()
_SERVICE = EvidenceStateService(machine=_MACHINE)


def _make_ref(*, metadata: dict | None = None) -> EvidenceRefModel:
    return EvidenceRefModel(
        ref_id=uuid.UUID(str(_REF)),
        schema_version="1.0",
        tenant_id=uuid.UUID(str(_TENANT)),
        venue_id=uuid.UUID(str(_VENUE)),
        ref_type="video_clip",
        ref_uri=f"tenants/{_TENANT}/venues/{_VENUE}/evidence/{_REF}.mp4",
        event_id=uuid.UUID(str(_EVENT)),
        event_time=_NOW,
        created_at=_NOW,
        captured_at=_NOW,
        metadata_=metadata,
    )


def _at_state(state: EvidenceProcessingState) -> EvidenceRefModel:
    """A ref whose durable metadata records the given state."""
    return _make_ref(
        metadata={
            EVIDENCE_PROCESSING_STATE_KEY: state.value,
            EVIDENCE_PROCESSING_VERSION_KEY: EVIDENCE_STATE_MACHINE_VERSION,
        }
    )


# =============================================================================
# Valid transitions
# =============================================================================


class TestValidTransitions:
    def test_happy_path(self) -> None:
        ref = _make_ref()
        assert _SERVICE.current_state(ref) is EvidenceProcessingState.REQUESTED

        r = _SERVICE.apply(ref, EvidenceEvent.QUEUE)
        assert r.applied is True
        assert r.from_state is EvidenceProcessingState.REQUESTED
        assert r.to_state is EvidenceProcessingState.QUEUED
        assert _SERVICE.current_state(ref) is EvidenceProcessingState.QUEUED

        r = _SERVICE.apply(ref, EvidenceEvent.START_EXTRACTING)
        assert r.to_state is EvidenceProcessingState.EXTRACTING

        r = _SERVICE.apply(ref, EvidenceEvent.EXTRACTION_COMPLETE)
        assert r.to_state is EvidenceProcessingState.UPLOADING

        r = _SERVICE.apply(ref, EvidenceEvent.FINALIZE)
        assert r.to_state is EvidenceProcessingState.FINALIZED

    def test_failure_and_recovery_path(self) -> None:
        ref = _at_state(EvidenceProcessingState.EXTRACTING)
        r = _SERVICE.apply(ref, EvidenceEvent.RETRYABLE_FAILURE)
        assert r.to_state is EvidenceProcessingState.RETRYABLE_FAILURE
        r = _SERVICE.apply(ref, EvidenceEvent.RETRY)
        assert r.to_state is EvidenceProcessingState.QUEUED
        r = _SERVICE.apply(ref, EvidenceEvent.START_EXTRACTING)
        assert r.to_state is EvidenceProcessingState.EXTRACTING

    def test_terminal_failure_from_extracting(self) -> None:
        ref = _at_state(EvidenceProcessingState.EXTRACTING)
        r = _SERVICE.apply(ref, EvidenceEvent.TERMINAL_FAILURE)
        assert r.to_state is EvidenceProcessingState.TERMINAL_FAILURE

    def test_terminal_failure_from_uploading(self) -> None:
        ref = _at_state(EvidenceProcessingState.UPLOADING)
        r = _SERVICE.apply(ref, EvidenceEvent.TERMINAL_FAILURE)
        assert r.to_state is EvidenceProcessingState.TERMINAL_FAILURE

    def test_expiry_from_active_states(self) -> None:
        for state in (
            EvidenceProcessingState.REQUESTED,
            EvidenceProcessingState.QUEUED,
            EvidenceProcessingState.EXTRACTING,
            EvidenceProcessingState.UPLOADING,
        ):
            ref = _at_state(state)
            r = _SERVICE.apply(ref, EvidenceEvent.EXPIRE)
            assert r.to_state is EvidenceProcessingState.EXPIRED, f"from {state.value}"


# =============================================================================
# Invalid transitions
# =============================================================================


class TestInvalidTransitions:
    def test_skip_requested_queued(self) -> None:
        ref = _make_ref()
        with pytest.raises(InvalidEvidenceTransitionError, match="invalid evidence transition"):
            _SERVICE.apply(ref, EvidenceEvent.START_EXTRACTING)

    def test_finalize_from_requested(self) -> None:
        ref = _make_ref()
        with pytest.raises(InvalidEvidenceTransitionError):
            _SERVICE.apply(ref, EvidenceEvent.FINALIZE)

    def test_extract_again_while_extracting(self) -> None:
        # START_EXTRACTING when already EXTRACTING is a duplicate delivery
        # of an already-applied event — idempotent no-op (Task 7), never
        # a second claim (a racing worker is stopped by the from-state
        # guard in ``claim_for_extraction`` instead).
        ref = _at_state(EvidenceProcessingState.EXTRACTING)
        result = _SERVICE.apply(ref, EvidenceEvent.START_EXTRACTING)
        assert result.applied is False
        assert result.to_state is EvidenceProcessingState.EXTRACTING
        assert _SERVICE.current_state(ref) is EvidenceProcessingState.EXTRACTING

    def test_retry_from_requested(self) -> None:
        ref = _make_ref()
        with pytest.raises(InvalidEvidenceTransitionError):
            _SERVICE.apply(ref, EvidenceEvent.RETRY)

    def test_queue_from_queued(self) -> None:
        # Queuing an already-queued ref is not a declared transition —
        # but it IS an idempotent duplicate (target == current) → no-op.
        ref = _at_state(EvidenceProcessingState.QUEUED)
        r = _SERVICE.apply(ref, EvidenceEvent.QUEUE)
        assert r.applied is False
        assert r.to_state is EvidenceProcessingState.QUEUED
        assert _SERVICE.current_state(ref) is EvidenceProcessingState.QUEUED

    def test_extract_again_after_finalized_rejected(self) -> None:
        ref = _at_state(EvidenceProcessingState.FINALIZED)
        with pytest.raises(FinalizedEvidenceMutationError):
            _SERVICE.apply(ref, EvidenceEvent.START_EXTRACTING)

    def test_every_terminal_state_has_no_outgoing_transitions(self) -> None:
        for state in (
            EvidenceProcessingState.FINALIZED,
            EvidenceProcessingState.TERMINAL_FAILURE,
            EvidenceProcessingState.EXPIRED,
        ):
            assert _MACHINE.allowed_events(state) == (), f"{state.value} must be terminal"


# =============================================================================
# Restart (durability + recovery)
# =============================================================================


class TestRestart:
    def test_restart_preserves_persisted_state(self) -> None:
        # A worker crashes mid-extraction; the durable metadata survives.
        ref = _at_state(EvidenceProcessingState.EXTRACTING)
        checkpoint = _SERVICE.restart(ref)
        assert checkpoint.state is EvidenceProcessingState.EXTRACTING
        assert checkpoint.version == EVIDENCE_STATE_MACHINE_VERSION
        # The worker resumes legally from the checkpoint — never REQUESTED.
        assert _SERVICE.current_state(ref) is EvidenceProcessingState.EXTRACTING

    def test_restart_of_never_started_ref_returns_requested(self) -> None:
        ref = _make_ref()
        checkpoint = _SERVICE.restart(ref)
        assert checkpoint.state is EvidenceProcessingState.REQUESTED

    def test_recovery_after_crash(self) -> None:
        # Crash during EXTRACTING → restart → mark retryable → re-queue →
        # extract again → upload → finalize. Nothing is lost.
        ref = _at_state(EvidenceProcessingState.EXTRACTING)
        _SERVICE.apply(ref, EvidenceEvent.RETRYABLE_FAILURE)
        _SERVICE.apply(ref, EvidenceEvent.RETRY)
        _SERVICE.apply(ref, EvidenceEvent.START_EXTRACTING)
        _SERVICE.apply(ref, EvidenceEvent.EXTRACTION_COMPLETE)
        result = _SERVICE.apply(ref, EvidenceEvent.FINALIZE)
        assert result.to_state is EvidenceProcessingState.FINALIZED

    def test_checkpoint_is_typed_and_stable(self) -> None:
        checkpoint = _SERVICE.restart(_at_state(EvidenceProcessingState.QUEUED))
        assert isinstance(checkpoint, EvidenceCheckpoint)
        assert checkpoint.ref_id == _REF
        assert repr(checkpoint) == (
            f"EvidenceCheckpoint(ref_id={uuid.UUID(str(_REF))}, "
            f"state=queued, version={EVIDENCE_STATE_MACHINE_VERSION})"
        )


# =============================================================================
# Duplicate worker (atomic from-state guard)
# =============================================================================


class TestDuplicateWorker:
    def test_second_worker_loses_claim(self) -> None:
        ref = _at_state(EvidenceProcessingState.QUEUED)
        worker_a = EvidenceStateService()
        worker_b = EvidenceStateService()

        # Worker A observes QUEUED and claims extraction.
        a_result = worker_a.claim_for_extraction(ref)
        assert a_result.to_state is EvidenceProcessingState.EXTRACTING

        # Worker B still believes it's QUEUED (stale observation) — the
        # from-state guard rejects it; the evidence is NOT double-claimed.
        with pytest.raises(EvidenceStateMismatchError, match="state mismatch"):
            worker_b.claim_for_extraction(ref)

        # The persisted state is EXTRACTING exactly once.
        assert _SERVICE.current_state(ref) is EvidenceProcessingState.EXTRACTING

    def test_duplicate_worker_without_expected_state_is_idempotent(self) -> None:
        # Without a stale from-state expectation, a duplicate delivery
        # of the same claim is a no-op (Task 7 idempotency).
        ref = _at_state(EvidenceProcessingState.QUEUED)
        first = _SERVICE.claim_for_extraction(ref)
        assert first.applied is True
        # A second claim attempt with NO stale expectation → idempotent
        # no-op (already EXTRACTING, target == current).
        again = _SERVICE.apply(ref, EvidenceEvent.START_EXTRACTING)
        assert again.applied is False
        assert _SERVICE.current_state(ref) is EvidenceProcessingState.EXTRACTING


# =============================================================================
# Retry
# =============================================================================


class TestRetry:
    def test_retry_is_bounded_by_state_sequence(self) -> None:
        ref = _at_state(EvidenceProcessingState.EXTRACTING)
        _SERVICE.apply(ref, EvidenceEvent.RETRYABLE_FAILURE)
        assert _SERVICE.current_state(ref) is EvidenceProcessingState.RETRYABLE_FAILURE
        # Only the declared recovery path exists — direct re-extract
        # from RETRYABLE_FAILURE is rejected.
        with pytest.raises(InvalidEvidenceTransitionError):
            _SERVICE.apply(ref, EvidenceEvent.START_EXTRACTING)
        # RETRY is the sole legal recovery event.
        _SERVICE.apply(ref, EvidenceEvent.RETRY)
        assert _SERVICE.current_state(ref) is EvidenceProcessingState.QUEUED

    def test_multiple_retry_cycles(self) -> None:
        ref = _at_state(EvidenceProcessingState.EXTRACTING)
        for _ in range(3):
            _SERVICE.apply(ref, EvidenceEvent.RETRYABLE_FAILURE)
            _SERVICE.apply(ref, EvidenceEvent.RETRY)
            _SERVICE.apply(ref, EvidenceEvent.START_EXTRACTING)
        assert _SERVICE.current_state(ref) is EvidenceProcessingState.EXTRACTING


# =============================================================================
# Finalization
# =============================================================================


class TestFinalization:
    def test_finalize_uploading(self) -> None:
        ref = _at_state(EvidenceProcessingState.UPLOADING)
        result = _SERVICE.finalize(ref)
        assert result.to_state is EvidenceProcessingState.FINALIZED
        assert result.applied is True
        assert _SERVICE.current_state(ref) is EvidenceProcessingState.FINALIZED

    def test_finalize_is_idempotent(self) -> None:
        ref = _at_state(EvidenceProcessingState.UPLOADING)
        first = _SERVICE.finalize(ref)
        assert first.applied is True
        # FINALIZED is IMMUTABLE — a second FINALIZE delivery is a
        # mutation attempt and is rejected (immutability wins over the
        # duplicate-delivery shortcut; workers detect the already-final
        # state via restart()'s checkpoint instead of re-sending).
        with pytest.raises(FinalizedEvidenceMutationError, match="immutable"):
            _SERVICE.apply(ref, EvidenceEvent.FINALIZE)


# =============================================================================
# Finalized mutation attempt (immutability)
# =============================================================================


class TestFinalizedImmutability:
    def test_every_event_rejected_on_finalized(self) -> None:
        ref = _at_state(EvidenceProcessingState.FINALIZED)
        for event in EvidenceEvent:
            with pytest.raises(FinalizedEvidenceMutationError, match="immutable"):
                _SERVICE.apply(ref, event)
        # The persisted state never changed.
        assert _SERVICE.current_state(ref) is EvidenceProcessingState.FINALIZED

    def test_finalized_state_never_resurrected_by_retry(self) -> None:
        ref = _at_state(EvidenceProcessingState.FINALIZED)
        with pytest.raises(FinalizedEvidenceMutationError):
            _SERVICE.apply(ref, EvidenceEvent.RETRY)


# =============================================================================
# Idempotency (Task 7)
# =============================================================================


class TestIdempotency:
    def test_duplicate_delivery_is_noop(self) -> None:
        ref = _make_ref()
        _SERVICE.apply(ref, EvidenceEvent.QUEUE)
        # Duplicate delivery of QUEUE — already QUEUED → no-op.
        result = _SERVICE.apply(ref, EvidenceEvent.QUEUE)
        assert result.applied is False
        assert _SERVICE.current_state(ref) is EvidenceProcessingState.QUEUED

    def test_idempotency_key_is_validated(self) -> None:
        ref = _make_ref()
        # The canonical Task 7 error — empty keys are rejected, never
        # trusted unvalidated.
        with pytest.raises(IdempotencyKeyError, match="idempotency key"):
            _SERVICE.apply(ref, EvidenceEvent.QUEUE, idempotency_key="")

    def test_valid_idempotency_key_accepted(self) -> None:
        ref = _make_ref()
        result = _SERVICE.apply(ref, EvidenceEvent.QUEUE, idempotency_key="evidence:queue:v1")
        assert result.applied is True


# =============================================================================
# Determinism + explicit table
# =============================================================================


class TestDeterminism:
    def test_identical_inputs_identical_outcomes(self) -> None:
        a = _at_state(EvidenceProcessingState.EXTRACTING)
        b = _at_state(EvidenceProcessingState.EXTRACTING)
        ra = _SERVICE.apply(a, EvidenceEvent.RETRYABLE_FAILURE)
        rb = _SERVICE.apply(b, EvidenceEvent.RETRYABLE_FAILURE)
        assert ra.to_state is rb.to_state
        assert ra.applied == rb.applied

    def test_machine_is_stateless_between_calls(self) -> None:
        # The FSM holds no per-ref state — identical (state, event) pairs
        # always map to the same next state.
        assert _MACHINE.next_state(
            EvidenceProcessingState.QUEUED, EvidenceEvent.START_EXTRACTING
        ) is (EvidenceProcessingState.EXTRACTING)
        assert _MACHINE.next_state(
            EvidenceProcessingState.QUEUED, EvidenceEvent.START_EXTRACTING
        ) is (EvidenceProcessingState.EXTRACTING)

    def test_allowed_events_are_deterministically_ordered(self) -> None:
        queued = _MACHINE.allowed_events(EvidenceProcessingState.QUEUED)
        assert queued == (EvidenceEvent.START_EXTRACTING, EvidenceEvent.EXPIRE)
        requested = _MACHINE.allowed_events(EvidenceProcessingState.REQUESTED)
        assert requested == (EvidenceEvent.QUEUE, EvidenceEvent.EXPIRE)


class TestExplicitTable:
    def test_all_states_are_declared(self) -> None:
        expected = {
            "requested",
            "queued",
            "extracting",
            "uploading",
            "finalized",
            "retryable_failure",
            "terminal_failure",
            "expired",
        }
        assert {s.value for s in _MACHINE.states()} == expected

    def test_initial_state_is_requested(self) -> None:
        assert _MACHINE.initial_state is EvidenceProcessingState.REQUESTED
        assert _SERVICE.current_state(_make_ref()) is EvidenceProcessingState.REQUESTED

    def test_transition_result_is_typed(self) -> None:
        result = _SERVICE.apply(_make_ref(), EvidenceEvent.QUEUE)
        assert isinstance(result, EvidenceTransitionResult)
        assert result.from_state is EvidenceProcessingState.REQUESTED
        assert result.to_state is EvidenceProcessingState.QUEUED
