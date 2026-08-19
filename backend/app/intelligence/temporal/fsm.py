"""Reusable deterministic FSM abstraction (Task 15 Step 1 §8/§9).

A small, pure, side-effect-free state machine that supports exactly one
question: given the current state and an event, what is the next state?

  - The legal transition table is declared once (``FsmRule`` tuples);
    anything not in the table is rejected with the typed
    ``InvalidTransitionError`` — no arbitrary state mutation.
  - The FSM performs NO side effects: it returns state names. Temporal
    policy (hysteresis, occlusion, grace) lives in the engine, which
    decides WHEN to emit FSM events; the FSM decides whether they are
    legal.
  - Identical input always returns the identical outcome (determinism).

This follows the project's existing state-machine convention
(``ConfigurationStateMachine``: explicit legal transitions, explicit
rejection of illegal ones) but is generic — the configuration FSM stays
in the configuration domain, this abstraction is reusable by every
future Task 15 FSM (presence, dwell, occupancy, waiting, movement).
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.intelligence.temporal.exceptions import InvalidTransitionError


@dataclass(frozen=True, slots=True)
class FsmRule:
    """One legal transition: ``from_state`` + ``event`` -> ``to_state``."""

    from_state: str
    event: str
    to_state: str


class DeterministicFsm:
    """Immutable deterministic FSM over a declared transition table.

    Args:
        name: FSM family name (e.g. ``"presence"``).
        version: semantics version (``TEMPORAL_ENGINE_VERSION``); state
            carries it so changed semantics never reinterpret old state.
        states: all legal states.
        initial_state: the state a fresh instance starts in.
        rules: the legal transition table (no duplicates allowed).
    """

    def __init__(
        self,
        *,
        name: str,
        version: str,
        states: tuple[str, ...],
        initial_state: str,
        rules: tuple[FsmRule, ...],
    ) -> None:
        if initial_state not in states:
            msg = f"FSM '{name}': initial_state {initial_state!r} is not a declared state"
            raise ValueError(msg)
        for rule in rules:
            if rule.from_state not in states or rule.to_state not in states:
                msg = (
                    f"FSM '{name}': rule {rule.from_state} --{rule.event}-> "
                    f"{rule.to_state} references an undeclared state"
                )
                raise ValueError(msg)
        self._name = name
        self._version = version
        self._states = states
        self._initial_state = initial_state
        self._lookup: dict[tuple[str, str], str] = {}
        for rule in rules:
            key = (rule.from_state, rule.event)
            if key in self._lookup:
                msg = f"FSM '{name}': duplicate rule for ({rule.from_state}, {rule.event})"
                raise ValueError(msg)
            self._lookup[key] = rule.to_state

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return self._version

    @property
    def states(self) -> tuple[str, ...]:
        return self._states

    @property
    def initial_state(self) -> str:
        return self._initial_state

    def can_transition(self, from_state: str, event: str) -> bool:
        """True when the table defines a transition for this state+event."""
        return (from_state, event) in self._lookup

    def next_state(self, from_state: str, event: str) -> str | None:
        """The next state for a legal transition, or None if illegal."""
        return self._lookup.get((from_state, event))

    def allowed_events(self, from_state: str) -> tuple[str, ...]:
        """All events legal from ``from_state`` (deterministic tuple order)."""
        return tuple(event for (state, event) in self._lookup if state == from_state)

    def transition(self, from_state: str, event: str) -> str:
        """Execute a transition, returning the next state.

        Raises:
            InvalidTransitionError: the transition is not in the table —
                the FSM contract forbids arbitrary state mutation.
        """
        next_state = self._lookup.get((from_state, event))
        if next_state is None:
            allowed = self.allowed_events(from_state)
            raise InvalidTransitionError(
                f"FSM '{self._name}': invalid transition from {from_state!r} "
                f"on event {event!r}; allowed: {', '.join(allowed) or 'none'}"
            )
        return next_state


__all__ = [
    "DeterministicFsm",
    "FsmRule",
]
