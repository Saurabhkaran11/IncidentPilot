"""Drains queued demo-request processing operations.

This is the "customer traffic" half of the demo: it invokes the registered
alias with a persisted request and records what actually happened. It is
also where a real AWS failure becomes evidence -- the FunctionError from a
live ``lambda:Invoke`` is captured verbatim and turned into an incident
event, rather than being simulated later (build brief section 5, step 3:
"actual AWS failures are captured as evidence, and requests remain
recoverable").
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from packages.aws.gateway import AwsGateway
from packages.domain.enums import Mode, OperationStatus, RequestStatus
from packages.storage.interface import ControlPlaneStore
from services.intake import open_or_attach_incident

logger = logging.getLogger("incidentpilot.worker.dispatch")

DISPATCH_SOURCE = "incidentpilot.demo-dispatcher"


def run_dispatch(
    store: ControlPlaneStore,
    gateway: AwsGateway,
    *,
    request_id: str,
    operation_id: str,
    mode: Mode,
) -> None:
    """Process one demo request through the alias."""
    demo_request = store.get_demo_request(request_id)
    if demo_request is None:
        logger.error("dispatch %s: request %s not found", operation_id, request_id)
        return

    app_config = store.get_app_config(demo_request.app_id)
    run = store.get_demo_run(demo_request.demo_run_id)
    if app_config is None or run is None:
        logger.error("dispatch %s: application or run missing", operation_id)
        return

    now = datetime.now(UTC)
    store.update_operation(operation_id, lambda op: op.model_copy(update={
        "status": OperationStatus.RUNNING, "started_at": now, "updated_at": now,
    }))
    store.update_demo_request(request_id, lambda r: r.model_copy(update={
        "status": RequestStatus.PROCESSING, "attempts": r.attempts + 1, "updated_at": now,
    }))

    payload = {"request_id": request_id, "document": demo_request.document.model_dump()}
    result = gateway.invoke(app_config.function_name, app_config.alias_name, payload)

    if result.function_error is None:
        body = result.payload or {}
        store.update_demo_request(request_id, lambda r: r.model_copy(update={
            "status": RequestStatus.COMPLETED,
            "result_id": body.get("result_id", request_id),
            "updated_at": datetime.now(UTC),
        }))
        store.update_operation(operation_id, lambda op: op.model_copy(update={
            "status": OperationStatus.SUCCEEDED, "finished_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC), "result": {"request_id": request_id, "outcome": "completed"},
        }))
        return

    # Failure path: the request stays durable and recoverable, and the
    # failure becomes an incident event (deduplicated downstream).
    store.update_demo_request(request_id, lambda r: r.model_copy(update={
        "status": RequestStatus.FAILED,
        "last_error_code": (result.error_message or "FunctionError")[:200],
        "updated_at": datetime.now(UTC),
    }))

    incident_id, investigation_op = open_or_attach_incident(
        store, gateway,
        workspace_id=run.workspace_id,
        app_id=demo_request.app_id,
        demo_run_id=demo_request.demo_run_id,
        source=DISPATCH_SOURCE,
        # Stable per (request, attempt): a retried dispatch of the same
        # attempt dedups, a genuinely new attempt does not.
        source_event_id=f"{request_id}:{demo_request.attempts + 1}",
        failure_kind="processor_invocation_failed",
        request_ids=[request_id],
        observed_at=datetime.now(UTC),
        mode=mode,
    )
    store.update_operation(operation_id, lambda op: op.model_copy(update={
        "status": OperationStatus.SUCCEEDED,  # the dispatch itself completed; the request failed
        "finished_at": datetime.now(UTC), "updated_at": datetime.now(UTC),
        "result": {
            "request_id": request_id, "outcome": "failed",
            "incident_id": incident_id, "investigation_operation_id": investigation_op,
        },
    }))
    logger.info("dispatch %s: request %s failed -> incident %s", operation_id, request_id, incident_id)
