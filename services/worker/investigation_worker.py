"""Drains queued investigation operations and runs the agent.

In fixture/local-live mode this is a polling loop over
``ControlPlaneStore`` operations (build brief section 7: "A separate local
worker drains persisted jobs ... Do not rely on FastAPI in-process
background tasks for durable execution"). The hosted/live deployment
replaces the polling loop with an SQS-triggered Lambda calling the same
``run_one`` function -- see infra/ and docs/decisions/0005-outbox-and-queues.md.
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from datetime import UTC, datetime

from agent.stub_investigator import MAX_DURATION_SECONDS, investigate_stub
from mcp_server.context import RunContext
from packages.aws.gateway import AwsGateway
from packages.domain.enums import (
    Assessment,
    IncidentState,
    OperationKind,
    OperationStatus,
    RecommendedAction,
)
from packages.domain.events import EventType
from packages.domain.ids import new_id
from packages.domain.models import Diagnosis, Hypothesis
from packages.storage.interface import ControlPlaneStore
from services.transitions import apply_transition

logger = logging.getLogger("incidentpilot.worker")

_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="investigation")


def run_investigation(
    store: ControlPlaneStore,
    gateway: AwsGateway,
    *,
    incident_id: str,
    operation_id: str,
    mode: str,
    data_dir: str,
    agent_mode: str,
) -> None:
    """Execute one investigation operation to completion.

    Called by the polling loop below, or directly by
    ``services/api`` tests that want synchronous behavior. Never raises for
    an investigation-domain failure (insufficient evidence, tool timeout) --
    those produce a normal ``needs_information``/``needs_attention``
    incident state. It only raises for programming errors.
    """
    incident = store.get_incident(incident_id)
    if incident is None:
        logger.error("investigation %s: incident %s not found", operation_id, incident_id)
        return

    store.update_operation(operation_id, lambda op: op.model_copy(update={
        "status": OperationStatus.RUNNING, "started_at": datetime.now(UTC), "updated_at": datetime.now(UTC)
    }))
    store.append_event(incident_id, EventType.INVESTIGATION_STARTED.value, {"operation_id": operation_id, "agent_mode": agent_mode})

    ctx = RunContext(
        incident_id=incident_id, run_id=operation_id, app_id=incident.app_id,
        demo_run_id=incident.demo_run_id, workspace_id=incident.workspace_id, mode=mode, data_dir=data_dir,
    )

    try:
        if agent_mode == "bedrock":
            future = _POOL.submit(_run_bedrock, ctx, incident_id)
        else:
            future = _POOL.submit(_run_stub, ctx, store, gateway, incident_id)
        diagnosis_out, tool_call_count, duration_ms, resolved_agent_mode, model_id = future.result(
            timeout=MAX_DURATION_SECONDS + 15
        )
    except concurrent.futures.TimeoutError:
        logger.warning("investigation %s timed out", operation_id)
        _fail_to_needs_attention(store, incident_id, operation_id, "investigation exceeded its time budget")
        return
    except Exception as exc:
        logger.exception("investigation %s failed", operation_id)
        _fail_to_needs_attention(store, incident_id, operation_id, str(exc))
        return

    diagnosis = Diagnosis(
        diagnosis_id=new_id("diag"),
        incident_id=incident_id,
        run_id=operation_id,
        assessment=diagnosis_out.assessment,
        summary=diagnosis_out.summary,
        hypotheses=[
            Hypothesis(
                description=h.description,
                supporting_evidence_ids=h.supporting_evidence_ids,
                contradicting_evidence_ids=h.contradicting_evidence_ids,
            )
            for h in diagnosis_out.hypotheses
        ],
        evidence_ids=diagnosis_out.evidence_ids,
        unknowns=diagnosis_out.unknowns,
        recommended_action=diagnosis_out.recommended_action,
        agent_mode=resolved_agent_mode,
        model_id=model_id,
        prompt_version="1.0",
        tool_call_count=tool_call_count,
        duration_ms=duration_ms,
        created_at=datetime.now(UTC),
    )
    store.put_diagnosis(diagnosis)
    store.append_event(
        incident_id, EventType.INVESTIGATION_COMPLETED.value,
        {
            "operation_id": operation_id, "assessment": diagnosis.assessment.value,
            "recommended_action": diagnosis.recommended_action.value,
            "tool_call_count": tool_call_count, "duration_ms": duration_ms,
        },
    )

    next_state = (
        IncidentState.AWAITING_APPROVAL
        if diagnosis.assessment == Assessment.SUPPORTED and diagnosis.recommended_action == RecommendedAction.ROLLBACK_ALIAS
        else IncidentState.NEEDS_INFORMATION
    )
    apply_transition(store, incident_id, next_state)

    store.update_operation(operation_id, lambda op: op.model_copy(update={
        "status": OperationStatus.SUCCEEDED, "finished_at": datetime.now(UTC), "updated_at": datetime.now(UTC),
        "result": {"diagnosis_id": diagnosis.diagnosis_id, "assessment": diagnosis.assessment.value},
    }))


def _run_stub(ctx: RunContext, store: ControlPlaneStore, gateway: AwsGateway, incident_id: str):
    diagnosis_out, tool_calls, duration_ms = investigate_stub(ctx, store, gateway, incident_id)
    return diagnosis_out, tool_calls, duration_ms, "stub", "stub-policy-v1"


def _run_bedrock(ctx: RunContext, incident_id: str):
    from agent.investigator import investigate

    result = investigate(ctx, incident_id)
    return result.diagnosis, result.tool_call_count, result.duration_ms, "bedrock", result.model_id


def _fail_to_needs_attention(store: ControlPlaneStore, incident_id: str, operation_id: str, message: str) -> None:
    store.append_event(incident_id, EventType.INCIDENT_NEEDS_ATTENTION.value, {"reason": message, "operation_id": operation_id})
    store.update_operation(operation_id, lambda op: op.model_copy(update={
        "status": OperationStatus.NEEDS_ATTENTION, "finished_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC), "error": message,
    }))
    apply_transition(store, incident_id, IncidentState.NEEDS_ATTENTION)


def poll_loop(store: ControlPlaneStore, gateway: AwsGateway, *, mode: str, data_dir: str, agent_mode: str, poll_seconds: float = 1.0) -> None:
    """Simple polling loop for local dev; see ``scripts/run_worker.py``.

    Assumes exactly one worker process (there is no atomic claim between
    "list queued" and "mark running" -- two concurrent pollers could both
    pick the same operation). The hosted deployment replaces this loop with
    SQS, whose visibility timeout gives a real single-claim guarantee; see
    docs/decisions/0005-outbox-and-queues.md.
    """
    logger.info("investigation worker started (mode=%s agent_mode=%s)", mode, agent_mode)
    while True:
        claimed = _claim_next_queued_investigation(store)
        if claimed is None:
            time.sleep(poll_seconds)
            continue
        incident_id, operation_id = claimed
        run_investigation(store, gateway, incident_id=incident_id, operation_id=operation_id, mode=mode, data_dir=data_dir, agent_mode=agent_mode)


def _claim_next_queued_investigation(store: ControlPlaneStore) -> tuple[str, str] | None:
    ops = store.list_operations_by_status(OperationKind.INVESTIGATION.value, OperationStatus.QUEUED.value, limit=1)
    if not ops:
        return None
    op = ops[0]
    return op.incident_id, op.operation_id
