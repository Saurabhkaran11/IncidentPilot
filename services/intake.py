"""Normalize a failure event into an incident, exactly once.

Both callers need identical behavior: ``POST /v1/incidents`` (from
EventBridge or an external dispatcher) and the local dispatch worker, which
observes the failure in-process and must not have to make an authenticated
HTTP call back into its own API just to report it. Duplicating this logic in
two places is how the two dedup layers drift apart, so it lives here.

Two independent dedup layers, because they catch different things:

* ``source_event_id`` -- exact redelivery of the *same* event (transport
  retry, at-least-once queue). Cheap, exact.
* active-incident lookup by ``(app, run, deployment fingerprint)`` -- three
  requests failing within a second are one incident, not three, even though
  each produces a distinct source event.
"""

from __future__ import annotations

from datetime import UTC, datetime

from packages.aws.gateway import AwsGateway
from packages.domain.enums import IncidentState, Mode, OperationKind, OperationStatus, RequestStatus
from packages.domain.events import EventType
from packages.domain.ids import new_id
from packages.domain.models import ImpactSummary, Incident, Operation
from packages.storage.interface import ControlPlaneStore
from services.transitions import apply_transition


def deployment_fingerprint(gateway: AwsGateway, function_name: str, alias_name: str, app_id: str) -> str:
    """Identifies "the thing that is currently deployed".

    Two failures share an incident only if they were produced by the same
    deployed artifact. Including the config fingerprint means a
    reconfiguration mid-incident opens a new one rather than silently
    folding new symptoms into an old diagnosis.
    """
    state = gateway.get_alias_state(function_name, alias_name)
    return f"{app_id}:{state.current_version}:{state.config_fingerprint[:12]}"


def open_or_attach_incident(
    store: ControlPlaneStore,
    gateway: AwsGateway,
    *,
    workspace_id: str,
    app_id: str,
    demo_run_id: str,
    source: str,
    source_event_id: str,
    failure_kind: str,
    request_ids: list[str],
    observed_at: datetime,
    mode: Mode,
) -> tuple[str, str | None]:
    """Returns ``(incident_id, investigation_operation_id | None)``.

    The operation id is ``None`` only when an investigation is already
    running for this incident -- in which case this event's evidence is
    absorbed by that run instead of starting an uncontrolled second one.
    """
    existing_incident_id = store.seen_source_event(source, source_event_id)
    if existing_incident_id:
        incident = store.get_incident(existing_incident_id)
        return existing_incident_id, incident.active_operation_id if incident else None

    app_config = store.get_app_config(app_id)
    fingerprint = deployment_fingerprint(gateway, app_config.function_name, app_config.alias_name, app_id)

    requests = store.list_demo_requests(demo_run_id)
    failed = sum(1 for r in requests if r.status is RequestStatus.FAILED)
    completed = sum(1 for r in requests if r.status is RequestStatus.COMPLETED)

    incident = store.find_active_incident(app_id, demo_run_id, fingerprint)
    now = datetime.now(UTC)

    if incident is None:
        incident = Incident(
            incident_id=new_id("inc"),
            workspace_id=workspace_id,
            app_id=app_id,
            demo_run_id=demo_run_id,
            fingerprint=fingerprint,
            state=IncidentState.DETECTED,
            mode=mode,
            version=1,
            created_at=now,
            updated_at=now,
            impact=ImpactSummary(
                failed_requests=failed,
                completed_requests=completed,
                first_failure_at=observed_at,
                mode=mode,
            ),
        )
        store.create_incident(incident)
        store.append_event(
            incident.incident_id,
            EventType.INCIDENT_DETECTED.value,
            {
                "source": source,
                "failure_kind": failure_kind,
                "request_ids": request_ids,
            },
        )
    else:
        # Attach: refresh the impact counts so the operator sees all three
        # failures, not just the one that opened the incident.
        store.update_incident(
            incident.incident_id,
            incident.version,
            lambda inc: inc.model_copy(
                update={
                    "impact": inc.impact.model_copy(
                        update={"failed_requests": failed, "completed_requests": completed}
                    ),
                    "version": inc.version + 1,
                    "updated_at": now,
                }
            ),
        )
        incident = store.get_incident(incident.incident_id)

    store.record_source_event(source, source_event_id, incident.incident_id)

    if incident.state is IncidentState.INVESTIGATING and incident.active_operation_id:
        return incident.incident_id, incident.active_operation_id

    return incident.incident_id, queue_investigation(store, incident.incident_id)


def queue_investigation(store: ControlPlaneStore, incident_id: str) -> str:
    """Persist the operation, then move the incident.

    The operation row is the durable record of intent: the worker finds it
    even if this process dies immediately afterwards (build brief section 7,
    "Persist a job before acknowledging a command").
    """
    now = datetime.now(UTC)
    operation_id = new_id("op")
    store.create_operation(
        Operation(
            operation_id=operation_id,
            kind=OperationKind.INVESTIGATION,
            incident_id=incident_id,
            status=OperationStatus.QUEUED,
            created_at=now,
            updated_at=now,
        )
    )
    current = store.get_incident(incident_id)
    store.update_incident(
        incident_id,
        current.version,
        lambda inc: inc.model_copy(
            update={
                "active_operation_id": operation_id,
                "version": inc.version + 1,
                "updated_at": now,
            }
        ),
    )
    apply_transition(store, incident_id, IncidentState.INVESTIGATING)
    return operation_id
