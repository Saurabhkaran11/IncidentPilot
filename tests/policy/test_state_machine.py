"""The incident lifecycle table and the one transition helper that writes it."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from packages.domain.enums import TERMINAL_INCIDENT_STATES, IncidentState, Mode
from packages.domain.ids import new_id
from packages.domain.models import ImpactSummary, Incident
from packages.domain.state_machine import (
    IllegalTransition,
    allowed_next_states,
    is_terminal,
    transition,
)
from services.transitions import apply_transition

S = IncidentState

# Transcribed from build brief section 9's lifecycle line.
BRIEF_EDGES = [
    (S.DETECTED, S.INVESTIGATING),
    (S.INVESTIGATING, S.NEEDS_INFORMATION),
    (S.INVESTIGATING, S.AWAITING_APPROVAL),
    (S.AWAITING_APPROVAL, S.REJECTED),
    (S.AWAITING_APPROVAL, S.APPLYING),
    (S.APPLYING, S.VERIFYING),
    (S.VERIFYING, S.RECOVERED),
    (S.VERIFYING, S.REPLAYING),
    (S.VERIFYING, S.SERVICE_RESTORED_PENDING_REQUESTS),
    (S.VERIFYING, S.NEEDS_ATTENTION),
    (S.REPLAYING, S.RECOVERED),
    (S.REPLAYING, S.SERVICE_RESTORED_PENDING_REQUESTS),
    (S.REPLAYING, S.NEEDS_ATTENTION),
]

ILLEGAL_EDGES = [
    (S.DETECTED, S.APPLYING),
    (S.DETECTED, S.RECOVERED),
    (S.INVESTIGATING, S.APPLYING),
    (S.INVESTIGATING, S.RECOVERED),
    (S.AWAITING_APPROVAL, S.VERIFYING),
    (S.AWAITING_APPROVAL, S.RECOVERED),
    (S.APPLYING, S.RECOVERED),
    (S.APPLYING, S.REPLAYING),
    (S.VERIFYING, S.APPLYING),
    (S.REPLAYING, S.VERIFYING),
]


@pytest.mark.parametrize(("current", "target"), BRIEF_EDGES, ids=lambda s: getattr(s, "value", s))
def test_every_lifecycle_edge_from_the_brief_is_allowed(current, target):
    assert transition(current, target) == target


@pytest.mark.parametrize(("current", "target"), ILLEGAL_EDGES, ids=lambda s: getattr(s, "value", s))
def test_an_edge_outside_the_lifecycle_is_rejected(current, target):
    with pytest.raises(IllegalTransition):
        transition(current, target)


@pytest.mark.parametrize("state", sorted(TERMINAL_INCIDENT_STATES), ids=lambda s: s.value)
def test_terminal_states_have_no_outgoing_edges(state):
    assert is_terminal(state)
    assert allowed_next_states(state) == frozenset()
    with pytest.raises(IllegalTransition):
        transition(state, IncidentState.INVESTIGATING)


def make_incident(world, state: IncidentState) -> Incident:
    now = datetime.now(UTC)
    incident = Incident(
        incident_id=new_id("inc"),
        workspace_id=world.app_config.workspace_id,
        app_id=world.app_config.app_id,
        demo_run_id=new_id("run"),
        fingerprint="document-demo:v7",
        state=state,
        mode=Mode.FIXTURE,
        version=1,
        created_at=now,
        updated_at=now,
        impact=ImpactSummary(failed_requests=3, completed_requests=0, mode=Mode.FIXTURE),
    )
    world.store.create_incident(incident)
    return incident


@pytest.mark.parametrize("state", [S.APPLYING, S.RECOVERED], ids=lambda s: s.value)
def test_re_entering_the_current_state_is_a_noop_not_needs_attention(world, state):
    """A duplicate queue delivery must not flip a recovered incident to needs_attention."""
    incident = make_incident(world, state)

    result = apply_transition(world.store, incident.incident_id, state)

    assert result.state == state
    assert result.version == incident.version
    assert world.store.get_incident(incident.incident_id).state == state


def test_an_illegal_transition_forces_needs_attention_rather_than_raising(world):
    incident = make_incident(world, IncidentState.DETECTED)

    result = apply_transition(world.store, incident.incident_id, IncidentState.RECOVERED)

    assert result.state == IncidentState.NEEDS_ATTENTION
