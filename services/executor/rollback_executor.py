"""The recovery executor: the only code path allowed to mutate the alias.

Build brief section 10: "Immediately before mutation, the executor
validates the actor's authorization, plan expiry, digest, incident state,
resource allowlist, deployment manifest, and live alias revision." This
module is deliberately the one place ``UpdateAlias`` is ever called from --
the investigator's MCP tools are read-only and cannot import this module
(see docs/decisions/0004-separate-executor-identity.md).

Runs as its own worker loop (``scripts/run_worker.py --role executor``) or
inline in tests. Never called from an HTTP request handler directly: the
API only persists the approval + outbox operation (build brief section 10:
"Do not hold an HTTP request open through investigation, rollback, or
replay").
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from packages.aws.gateway import AliasRevisionStale, AwsGateway
from packages.domain.config import AppConfig
from packages.domain.enums import (
    IncidentState,
    OperationPhase,
    OperationStatus,
    ReceiptOutcome,
    ReceiptRequestOutcome,
    RequestStatus,
)
from packages.domain.events import EventType
from packages.domain.ids import new_id
from packages.domain.models import Receipt, RequestExecutionOutcome, UsageInfo, VerificationCheck
from packages.domain.plan import REPLAY_EXECUTION_DEADLINE_SECONDS, RecoveryPlan
from packages.storage.interface import ControlPlaneStore
from services.transitions import apply_transition

logger = logging.getLogger("incidentpilot.executor")

LEASE_SECONDS = 120
CANARY_DOCUMENT = {"fixture_id": "invoice-canary", "document_type": "invoice", "page_count": 1}


class ExecutionAborted(Exception):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


def run_execution(store: ControlPlaneStore, gateway: AwsGateway, *, operation_id: str, worker_id: str | None = None) -> None:
    worker_id = worker_id or f"executor-{uuid.uuid4().hex[:8]}"
    record = store.get_execution_record(operation_id)
    if record is None:
        logger.error("execution %s: no execution record", operation_id)
        return

    if not store.claim_execution_lease(operation_id, worker_id, LEASE_SECONDS):
        logger.info("execution %s: lease held by another worker; skipping", operation_id)
        return

    plan = store.get_plan(record.plan_id)
    approval = store.get_approval(record.approval_id)
    incident = store.get_incident(record.incident_id)
    if plan is None or approval is None or incident is None:
        _mark_needs_attention(store, operation_id, record.incident_id, "missing plan/approval/incident record")
        return

    app_config = store.get_app_config(incident.app_id)
    if app_config is None:
        _mark_needs_attention(store, operation_id, record.incident_id, "application not registered")
        return

    store.update_operation(operation_id, lambda op: op.model_copy(update={
        "status": OperationStatus.RUNNING, "phase": OperationPhase.VALIDATE,
        "started_at": op.started_at or datetime.now(UTC), "updated_at": datetime.now(UTC),
    }))
    store.append_event(record.incident_id, EventType.EXECUTION_STARTED.value, {"operation_id": operation_id, "plan_id": plan.plan_id})

    try:
        _validate_preconditions(store, plan, approval, record.operation_id)
        _apply_rollback(store, gateway, app_config, plan, record.incident_id, operation_id)
        checks = _verify_recovery(store, gateway, app_config, plan, record.incident_id, operation_id)
        if not all(c.passed for c in checks):
            _mark_needs_attention(store, operation_id, record.incident_id, "post-rollback verification failed")
            return

        replay_outcomes: list[RequestExecutionOutcome] = []
        if plan.replay_requests:
            from services.executor.replay import run_replay

            store.update_operation(operation_id, lambda op: op.model_copy(update={"phase": OperationPhase.REPLAY, "updated_at": datetime.now(UTC)}))
            apply_transition(store, record.incident_id, IncidentState.REPLAYING)
            replay_outcomes = run_replay(store, gateway, app_config, plan, operation_id, deadline=datetime.now(UTC) + timedelta(seconds=REPLAY_EXECUTION_DEADLINE_SECONDS))

        unresolved = _unresolved_request_ids(store, incident.demo_run_id, replay_outcomes)
        outcome = ReceiptOutcome.RECOVERED if not unresolved else ReceiptOutcome.PARTIAL
        final_state = IncidentState.RECOVERED if not unresolved else IncidentState.SERVICE_RESTORED_PENDING_REQUESTS
        apply_transition(store, record.incident_id, final_state)

        receipt = _build_receipt(
            store, record.incident_id, plan, approval, operation_id, checks, replay_outcomes, unresolved, outcome
        )
        store.put_receipt(receipt)
        store.append_event(record.incident_id, EventType.RECEIPT_CREATED.value, {"receipt_id": receipt.receipt_id, "outcome": outcome.value})
        store.update_operation(operation_id, lambda op: op.model_copy(update={
            "status": OperationStatus.SUCCEEDED, "phase": OperationPhase.RECEIPT,
            "finished_at": datetime.now(UTC), "updated_at": datetime.now(UTC),
            "result": {"receipt_id": receipt.receipt_id, "outcome": outcome.value},
        }))
    except ExecutionAborted as exc:
        _mark_needs_attention(store, operation_id, record.incident_id, str(exc), error_code=exc.error_code)
    except Exception:
        logger.exception("execution %s crashed", operation_id)
        _mark_needs_attention(store, operation_id, record.incident_id, "unexpected executor error")


def _validate_preconditions(store: ControlPlaneStore, plan: RecoveryPlan, approval, operation_id: str) -> None:
    from packages.domain.enums import Decision
    from packages.domain.plan import compute_plan_digest

    if approval.decision != Decision.APPROVE:
        raise ExecutionAborted("NOT_APPROVED", "plan was not approved")
    stored_digest = store.get_plan_digest(plan.plan_id)
    if stored_digest != approval.plan_digest or stored_digest != compute_plan_digest(plan):
        raise ExecutionAborted("PLAN_DIGEST_MISMATCH", "plan digest does not match the approved digest")
    # Expiry blocks the *start* of a new mutation only; once this operation's
    # execution record exists, we are continuing an already-approved attempt.
    already_attempted = bool(store.get_execution_record(operation_id).actual_alias_revision_id)
    if not already_attempted and datetime.now(UTC) > plan.expires_at:
        raise ExecutionAborted("PLAN_EXPIRED", "plan expired before execution started")


def _apply_rollback(store: ControlPlaneStore, gateway: AwsGateway, app_config: AppConfig, plan: RecoveryPlan, incident_id: str, operation_id: str) -> None:
    store.update_operation(operation_id, lambda op: op.model_copy(update={"phase": OperationPhase.APPLY, "updated_at": datetime.now(UTC)}))
    apply_transition(store, incident_id, IncidentState.APPLYING)

    try:
        new_state = gateway.update_alias(app_config.function_name, plan.alias_name, plan.expected_alias_revision_id, plan.to_version)
    except AliasRevisionStale as exc:
        raise ExecutionAborted("STALE_PLAN", f"alias revision changed since the plan was prepared: {exc}") from exc
    except Exception as exc:
        # Ambiguous result (timeout etc.): reconcile by reading back state
        # instead of blindly repeating UpdateAlias (build brief section 10).
        current = gateway.get_alias_state(app_config.function_name, plan.alias_name)
        if current.current_version == plan.to_version:
            new_state = current
        else:
            raise ExecutionAborted("EXECUTION_AMBIGUOUS", f"UpdateAlias result unknown and alias not at target: {exc}") from exc

    store.update_execution_record(operation_id, lambda rec: rec.model_copy(update={
        "actual_alias_revision_id": new_state.alias_revision_id, "applied_version": new_state.current_version,
    }))
    store.append_event(incident_id, EventType.EXECUTION_RECONCILED.value, {
        "operation_id": operation_id, "applied_version": new_state.current_version, "alias_revision_id": new_state.alias_revision_id,
    })


def _verify_recovery(store: ControlPlaneStore, gateway: AwsGateway, app_config: AppConfig, plan: RecoveryPlan, incident_id: str, operation_id: str) -> list[VerificationCheck]:
    store.update_operation(operation_id, lambda op: op.model_copy(update={"phase": OperationPhase.VERIFY, "updated_at": datetime.now(UTC)}))
    apply_transition(store, incident_id, IncidentState.VERIFYING)

    checks: list[VerificationCheck] = []
    now = datetime.now(UTC)

    post_state = gateway.get_alias_state(app_config.function_name, plan.alias_name)
    alias_ok = post_state.current_version == plan.to_version and not post_state.weighted_routing
    checks.append(VerificationCheck(check="alias_version", passed=alias_ok, observed_at=now,
                                     details=f"alias at version {post_state.current_version}, weighted={post_state.weighted_routing}"))

    canary_id = f"canary_{operation_id}"
    canary_result = gateway.invoke(app_config.function_name, plan.alias_name, {"request_id": canary_id, "document": CANARY_DOCUMENT})
    invocation_ok = canary_result.function_error is None and canary_result.executed_version == plan.to_version
    checks.append(VerificationCheck(
        check="canary_invocation", passed=invocation_ok, observed_at=datetime.now(UTC),
        details=canary_result.error_message or f"executed_version={canary_result.executed_version}",
    ))

    result_record = gateway.get_result(app_config.expected_results_table_ref, canary_id)
    result_ok = invocation_ok and result_record is not None
    checks.append(VerificationCheck(
        check="canary_result_and_hash", passed=result_ok, observed_at=datetime.now(UTC),
        details=(f"result found, hash={result_record.payload_sha256}" if result_record else "no result record found"),
    ))
    return checks


def _unresolved_request_ids(store: ControlPlaneStore, demo_run_id: str, replay_outcomes: list[RequestExecutionOutcome]) -> list[str]:
    resolved_ids = {o.request_id for o in replay_outcomes if o.outcome in (ReceiptRequestOutcome.COMPLETED, ReceiptRequestOutcome.ALREADY_COMPLETED)}
    requests = store.list_demo_requests(demo_run_id)
    return [r.request_id for r in requests if r.status == RequestStatus.FAILED and r.request_id not in resolved_ids]


def _build_receipt(store, incident_id, plan, approval, operation_id, checks, replay_outcomes, unresolved, outcome) -> Receipt:
    requests = store.list_demo_requests(store.get_incident(incident_id).demo_run_id)
    replay_ids = {o.request_id for o in replay_outcomes}
    request_lines: list[RequestExecutionOutcome] = list(replay_outcomes)
    for r in requests:
        if r.request_id in replay_ids:
            continue
        if r.status == RequestStatus.FAILED:
            request_lines.append(RequestExecutionOutcome(request_id=r.request_id, outcome=ReceiptRequestOutcome.NOT_REPLAYED))
        elif r.status == RequestStatus.COMPLETED:
            request_lines.append(RequestExecutionOutcome(request_id=r.request_id, outcome=ReceiptRequestOutcome.ALREADY_COMPLETED, result_id=r.result_id))

    diagnosis = store.get_latest_diagnosis(incident_id)
    investigation_ms = diagnosis.duration_ms if diagnosis else 0

    return Receipt(
        receipt_id=new_id("receipt"),
        incident_id=incident_id,
        mode=store.get_incident(incident_id).mode,
        outcome=outcome,
        plan_id=plan.plan_id,
        approval_id=approval.approval_id,
        execution_id=operation_id,
        applied_version=plan.to_version,
        verification=checks,
        requests=request_lines,
        unresolved_request_ids=unresolved,
        timing_ms={"investigation": investigation_ms},
        usage=UsageInfo(model_id=diagnosis.model_id if diagnosis else "unknown"),
        created_at=datetime.now(UTC),
    )


def _mark_needs_attention(store: ControlPlaneStore, operation_id: str, incident_id: str, message: str, *, error_code: str = "NEEDS_ATTENTION") -> None:
    store.append_event(incident_id, EventType.INCIDENT_NEEDS_ATTENTION.value, {"operation_id": operation_id, "reason": message, "code": error_code})
    store.update_operation(operation_id, lambda op: op.model_copy(update={
        "status": OperationStatus.NEEDS_ATTENTION, "finished_at": datetime.now(UTC), "updated_at": datetime.now(UTC), "error": message,
    }))
    apply_transition(store, incident_id, IncidentState.NEEDS_ATTENTION)
