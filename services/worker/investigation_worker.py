"""Drains queued investigation operations and runs the agent.

In fixture/local-live mode this is a polling loop over
``ControlPlaneStore`` operations (build brief section 7: "A separate local
worker drains persisted jobs ... Do not rely on FastAPI in-process
background tasks for durable execution"). ``services/worker/loop.py`` drains it; the hosted deployment replaces
that loop with an SQS-triggered Lambda calling ``run_investigation``
directly -- see infra/ and docs/decisions/0005-outbox-and-queues.md.
"""

from __future__ import annotations

import concurrent.futures
import logging
from datetime import UTC, datetime

from agent.stub_investigator import MAX_DURATION_SECONDS, investigate_stub
from mcp_server.context import RunContext
from packages.aws.gateway import AwsGateway
from packages.domain.enums import (
    Assessment,
    IncidentState,
    OperationStatus,
    RecommendedAction,
    RequestStatus,
)
from packages.domain.events import EventType
from packages.domain.ids import new_id
from packages.domain.models import Diagnosis, Hypothesis
from packages.domain.plan import MAX_REPLAY_REQUESTS
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

    plan_id = None
    if diagnosis.assessment is Assessment.SUPPORTED and diagnosis.recommended_action is RecommendedAction.ROLLBACK_ALIAS:
        plan_id = _build_plan(store, gateway, ctx, incident_id, diagnosis)

    apply_transition(
        store, incident_id,
        IncidentState.AWAITING_APPROVAL if plan_id else IncidentState.NEEDS_INFORMATION,
    )

    store.update_operation(operation_id, lambda op: op.model_copy(update={
        "status": OperationStatus.SUCCEEDED, "finished_at": datetime.now(UTC), "updated_at": datetime.now(UTC),
        "result": {"diagnosis_id": diagnosis.diagnosis_id, "assessment": diagnosis.assessment.value, "plan_id": plan_id},
    }))


def _build_plan(
    store: ControlPlaneStore, gateway: AwsGateway, ctx: RunContext, incident_id: str, diagnosis
) -> str | None:
    """Turn a supported diagnosis into an approvable plan, or explain why not.

    The agent recommended; this decides. Evidence is re-read *now* rather
    than reused from the diagnosis, so the alias revision baked into the
    plan is current -- that revision is the precondition the executor will
    check again before it mutates anything.
    """
    from mcp_server import tools as evidence_tools
    from packages.domain.plan import canonical_bytes, compute_plan_digest
    from packages.domain.plan_policy import PlanPolicyRejection, ReleaseDiffSnapshot, evaluate

    incident = store.get_incident(incident_id)
    app_config = store.get_app_config(incident.app_id)
    diff_resp = evidence_tools.get_release_diff(ctx, store, gateway, incident_id)
    if diff_resp["status"] == "error" or diff_resp["data"] is None:
        store.append_event(incident_id, EventType.PLAN_INVALIDATED.value, {
            "reason": "could not re-read deployment state while building the plan",
        })
        return None

    d = diff_resp["data"]
    # Only requests that actually failed are eligible, capped at the
    # replay bound. The operator cannot add to this list later; the plan
    # digest is computed over exactly these IDs and hashes.
    failed = [r for r in store.list_demo_requests(incident.demo_run_id) if r.status is RequestStatus.FAILED]
    replay_refs = [(r.request_id, r.payload_sha256) for r in failed[:MAX_REPLAY_REQUESTS]]

    outcome = evaluate(
        incident_id=incident_id,
        app_config=app_config,
        diagnosis_recommended_action=diagnosis.recommended_action,
        diagnosis_evidence_ids=diagnosis.evidence_ids,
        diff=ReleaseDiffSnapshot(
            alias_name=d["alias_name"], current_version=d["current_version"],
            known_good_version=d["known_good_version"], alias_revision_id=d["alias_revision_id"],
            weighted_routing=d["weighted_routing"], schema_compatible=d["schema_compatible"],
            changes=d["changes"], processor_role_matches_manifest=d["processor_role_matches_manifest"],
        ),
        known_good_entry=store.get_known_good_deployment(incident.app_id),
        replay_request_ids=replay_refs,
    )
    if isinstance(outcome, PlanPolicyRejection):
        logger.info("incident %s: plan policy refused (%s)", incident_id, outcome.reason_code)
        store.append_event(incident_id, EventType.PLAN_INVALIDATED.value, {
            "reason_code": outcome.reason_code, "message": outcome.message,
        })
        return None

    digest = compute_plan_digest(outcome)
    store.put_plan(outcome, canonical_bytes(outcome), digest)
    store.append_event(incident_id, EventType.PLAN_CREATED.value, {
        "plan_id": outcome.plan_id, "from_version": outcome.from_version,
        "to_version": outcome.to_version, "replay_request_count": len(outcome.replay_requests),
        "expires_at": outcome.expires_at.isoformat(),
    })
    current = store.get_incident(incident_id)
    store.update_incident(
        incident_id, current.version,
        lambda inc: inc.model_copy(update={
            "active_plan_id": outcome.plan_id, "version": inc.version + 1, "updated_at": datetime.now(UTC),
        }),
    )
    return outcome.plan_id


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
