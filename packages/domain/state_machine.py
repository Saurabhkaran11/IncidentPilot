"""The incident lifecycle, as one explicit transition table.

Per the build brief: "Terminal operational success is decided by checks, not
the LLM." This module is the single place that decides whether a requested
transition is legal. Application code (services/api, services/worker,
services/executor) must route every incident state change through
``transition()`` instead of assigning ``incident.state`` directly, so that an
illegal transition is a loud ``IllegalTransition`` exception, not a silent
bug discovered later in a demo.
"""

from __future__ import annotations

from packages.domain.enums import IncidentState

# Adjacency list transcribed directly from the build brief:
#   detected -> investigating
#   investigating -> needs_information | awaiting_approval
#   awaiting_approval -> rejected | applying
#   applying -> verifying
#   verifying -> recovered | replaying | service_restored_pending_requests | needs_attention
#   replaying -> recovered | service_restored_pending_requests | needs_attention
#   needs_information -> investigating (new investigation after more evidence)
_TRANSITIONS: dict[IncidentState, frozenset[IncidentState]] = {
    IncidentState.DETECTED: frozenset({IncidentState.INVESTIGATING}),
    IncidentState.INVESTIGATING: frozenset(
        {IncidentState.NEEDS_INFORMATION, IncidentState.AWAITING_APPROVAL, IncidentState.NEEDS_ATTENTION}
    ),
    IncidentState.NEEDS_INFORMATION: frozenset({IncidentState.INVESTIGATING}),
    IncidentState.AWAITING_APPROVAL: frozenset(
        {IncidentState.REJECTED, IncidentState.APPLYING, IncidentState.INVESTIGATING}
    ),
    IncidentState.APPLYING: frozenset({IncidentState.VERIFYING, IncidentState.NEEDS_ATTENTION}),
    IncidentState.VERIFYING: frozenset(
        {
            IncidentState.RECOVERED,
            IncidentState.REPLAYING,
            IncidentState.SERVICE_RESTORED_PENDING_REQUESTS,
            IncidentState.NEEDS_ATTENTION,
        }
    ),
    IncidentState.REPLAYING: frozenset(
        {
            IncidentState.RECOVERED,
            IncidentState.SERVICE_RESTORED_PENDING_REQUESTS,
            IncidentState.NEEDS_ATTENTION,
        }
    ),
    # Terminal states listed with an empty transition set: reaching them ends
    # this incident's lifecycle. A later investigation creates a *new*
    # incident/run rather than reopening this one -- see build brief section 9
    # ("Expired or stale unexecuted plans require a new investigation/plan").
    IncidentState.REJECTED: frozenset(),
    IncidentState.RECOVERED: frozenset(),
    IncidentState.SERVICE_RESTORED_PENDING_REQUESTS: frozenset(),
    IncidentState.NEEDS_ATTENTION: frozenset(),
}


class IllegalTransition(ValueError):
    def __init__(self, current: IncidentState, target: IncidentState) -> None:
        self.current = current
        self.target = target
        super().__init__(f"cannot transition incident from {current!r} to {target!r}")


def allowed_next_states(current: IncidentState) -> frozenset[IncidentState]:
    return _TRANSITIONS[current]


def can_transition(current: IncidentState, target: IncidentState) -> bool:
    return target in _TRANSITIONS[current]


def transition(current: IncidentState, target: IncidentState) -> IncidentState:
    """Validate and return the new state, or raise ``IllegalTransition``."""
    if not can_transition(current, target):
        raise IllegalTransition(current, target)
    return target


def is_terminal(state: IncidentState) -> bool:
    return len(_TRANSITIONS[state]) == 0
