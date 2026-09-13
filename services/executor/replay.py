"""Bounded replay of explicitly-approved failed requests (build brief section 11, P1).

Only the request IDs baked into the approved plan may be replayed -- never
"all currently failed requests" -- and only within the plan's own 5-minute
execution deadline once replay starts. Stops on a changed application
version, a blown deadline, or a policy violation; it must never enlarge the
batch to compensate.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from packages.aws.gateway import AwsGateway
from packages.domain.config import AppConfig
from packages.domain.enums import ReceiptRequestOutcome, RequestStatus
from packages.domain.events import EventType
from packages.domain.models import RequestExecutionOutcome
from packages.domain.plan import RecoveryPlan
from packages.storage.interface import ControlPlaneStore

logger = logging.getLogger("incidentpilot.executor.replay")


def run_replay(
    store: ControlPlaneStore,
    gateway: AwsGateway,
    app_config: AppConfig,
    plan: RecoveryPlan,
    operation_id: str,
    *,
    deadline: datetime,
) -> list[RequestExecutionOutcome]:
    incident_id = plan.incident_id
    store.append_event(incident_id, EventType.REPLAY_STARTED.value, {
        "operation_id": operation_id, "request_count": len(plan.replay_requests),
    })

    outcomes: list[RequestExecutionOutcome] = []
    for ref in plan.replay_requests:
        if datetime.now(UTC) > deadline:
            logger.warning("replay %s: deadline exceeded, stopping (processed %d/%d)", operation_id, len(outcomes), len(plan.replay_requests))
            break

        current = gateway.get_alias_state(app_config.function_name, plan.alias_name)
        if current.current_version != plan.to_version:
            logger.warning("replay %s: alias moved to %s (expected %s), stopping", operation_id, current.current_version, plan.to_version)
            break

        demo_request = store.get_demo_request(ref.request_id)
        if demo_request is None or demo_request.payload_sha256 != ref.payload_sha256:
            outcomes.append(RequestExecutionOutcome(request_id=ref.request_id, outcome=ReceiptRequestOutcome.CONFLICT, details="request not found or payload changed since plan was built"))
            continue

        payload = {
            "request_id": ref.request_id,
            "document": {
                "fixture_id": demo_request.document.fixture_id,
                "document_type": demo_request.document.document_type,
                "page_count": demo_request.document.page_count,
            },
        }
        invoke_result = gateway.invoke(app_config.function_name, plan.alias_name, payload)

        if invoke_result.function_error is not None:
            outcome = RequestExecutionOutcome(request_id=ref.request_id, outcome=ReceiptRequestOutcome.STILL_FAILED, details=invoke_result.error_message)
            store.update_demo_request(ref.request_id, lambda r: r.model_copy(update={"attempts": r.attempts + 1, "updated_at": datetime.now(UTC)}))
        else:
            body = invoke_result.payload or {}
            if body.get("conflict"):
                outcome = RequestExecutionOutcome(request_id=ref.request_id, outcome=ReceiptRequestOutcome.CONFLICT, result_id=ref.request_id)
                new_status = RequestStatus.NEEDS_ATTENTION
            elif body.get("already_completed"):
                outcome = RequestExecutionOutcome(request_id=ref.request_id, outcome=ReceiptRequestOutcome.ALREADY_COMPLETED, result_id=ref.request_id)
                new_status = RequestStatus.COMPLETED
            else:
                outcome = RequestExecutionOutcome(request_id=ref.request_id, outcome=ReceiptRequestOutcome.COMPLETED, result_id=ref.request_id)
                new_status = RequestStatus.COMPLETED
            store.update_demo_request(ref.request_id, lambda r, s=new_status: r.model_copy(update={
                "status": s, "result_id": ref.request_id, "attempts": r.attempts + 1, "updated_at": datetime.now(UTC)
            }))

        outcomes.append(outcome)
        store.append_event(incident_id, EventType.REQUEST_RECONCILED.value, {
            "operation_id": operation_id, "request_id": ref.request_id, "outcome": outcome.outcome.value,
        })

    # Any approved request never attempted (deadline/version-change stop) is
    # reported honestly as not_replayed, never silently dropped.
    attempted_ids = {o.request_id for o in outcomes}
    for ref in plan.replay_requests:
        if ref.request_id not in attempted_ids:
            outcomes.append(RequestExecutionOutcome(request_id=ref.request_id, outcome=ReceiptRequestOutcome.NOT_REPLAYED))

    return outcomes
