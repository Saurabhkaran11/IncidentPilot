"""One place that moves an incident between states.

The worker and the executor both need "validate the edge, bump the version,
write it, tolerate a concurrent writer". Duplicating that produced a real
bug: the executor re-entered ``applying`` (already set by the API when it
queued the operation), the strict state machine rejected the self-edge, and
the incident was coerced to ``needs_attention`` *while its receipt said
recovered* -- precisely the contradiction the build brief forbids.

Re-entering the state you are already in is not an illegal transition; it is
what a duplicate queue delivery or a retried worker looks like (build brief
section 14: "Worker receives duplicate job -> one claimed mutation; status
reconciled on retry"). So that case is an explicit no-op here, and
``state_machine._TRANSITIONS`` stays strict about real edges.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from packages.domain.enums import IncidentState
from packages.domain.models import Incident
from packages.domain.state_machine import IllegalTransition, transition
from packages.storage.interface import ConcurrencyConflict, ControlPlaneStore

logger = logging.getLogger("incidentpilot.transitions")


def apply_transition(store: ControlPlaneStore, incident_id: str, target: IncidentState) -> Incident | None:
    """Move ``incident_id`` to ``target``. Returns the updated incident.

    An illegal edge is a programming error, not an operational one: it is
    logged and forced to ``needs_attention`` so a human looks at it, rather
    than raising into a worker loop that would then retry forever.
    """
    incident = store.get_incident(incident_id)
    if incident is None:
        logger.error("transition: incident %s not found", incident_id)
        return None

    if incident.state == target:
        return incident

    try:
        transition(incident.state, target)
    except IllegalTransition:
        logger.error(
            "incident %s: illegal transition %s -> %s; forcing needs_attention",
            incident_id,
            incident.state.value,
            target.value,
        )
        if incident.state == IncidentState.NEEDS_ATTENTION:
            return incident
        target = IncidentState.NEEDS_ATTENTION

    try:
        return store.update_incident(
            incident_id,
            incident.version,
            lambda inc: inc.model_copy(
                update={
                    "state": target,
                    "version": inc.version + 1,
                    "updated_at": datetime.now(UTC),
                }
            ),
        )
    except ConcurrencyConflict:
        logger.warning("incident %s changed concurrently during transition to %s", incident_id, target.value)
        return store.get_incident(incident_id)
