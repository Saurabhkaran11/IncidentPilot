#!/usr/bin/env python
"""End-to-end fixture-mode walkthrough of build brief section 5, step by step.

No FastAPI, no AWS credentials -- this drives the domain/storage/agent/
executor layers directly, so it is both a developer smoke test and a
readable narration of the exact demonstration script for anyone reviewing
the code. ``services/api`` calls the identical functions from HTTP handlers;
this script is not a separate mock path.

    python scripts/run_fixture_demo.py [--data-dir data] [--reset]
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_server.context import RunContext
from packages.aws.fixture_gateway import FixtureAwsGateway
from packages.domain.enums import (
    Decision,
    IncidentState,
    Mode,
    OperationKind,
    OperationStatus,
    RequestStatus,
)
from packages.domain.ids import new_id
from packages.domain.models import (
    Approval,
    DemoDocument,
    DemoRequest,
    DemoRun,
    ExecutionRecord,
    ImpactSummary,
    Incident,
    Operation,
)
from packages.domain.plan import compute_plan_digest
from packages.domain.plan_policy import ReleaseDiffSnapshot, evaluate
from packages.storage.sqlite_store import SqliteControlPlaneStore
from services.executor.rollback_executor import run_execution
from services.worker.investigation_worker import run_investigation


def log(msg: str) -> None:
    print(f"[demo] {msg}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--reset", action="store_true", help="wipe the data dir before seeding")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if args.reset and data_dir.exists():
        shutil.rmtree(data_dir)

    import scripts.seed_fixture as seed_fixture

    seed_fixture.seed(data_dir)

    store = SqliteControlPlaneStore(data_dir / "control_plane.db")
    gateway = FixtureAwsGateway(data_dir / "fixture_aws.db")
    app_config = store.get_app_config("document-demo")
    assert app_config is not None

    # Step 1: three fixture requests, persisted before processing (build
    # brief step 3).
    run = DemoRun(demo_run_id=new_id("run"), workspace_id=app_config.workspace_id, app_id=app_config.app_id, mode=Mode.FIXTURE, created_at=datetime.now(UTC))
    store.create_demo_run(run)
    log(f"created demo run {run.demo_run_id}")

    request_ids = []
    for i, fixture_id in enumerate(["invoice-example-a", "invoice-example-b", "invoice-example-c"]):
        from demo.workload.processor import canonicalize_input, payload_sha256

        req_id = new_id("docreq")
        document = DemoDocument(fixture_id=fixture_id, document_type="invoice", page_count=2)
        canonical = canonicalize_input(req_id, document.model_dump())
        digest = payload_sha256(canonical)
        store.create_demo_request(DemoRequest(
            request_id=req_id, demo_run_id=run.demo_run_id, app_id=app_config.app_id, document=document,
            payload_sha256=digest, status=RequestStatus.ACCEPTED, created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        ))
        request_ids.append(req_id)
    log(f"submitted {len(request_ids)} fixture requests: {request_ids}")

    # Step 2: fault injection -- point the alias at the faulty version B.
    gateway.force_set_alias(app_config.function_name, app_config.alias_name, "7")
    log("fault injected: alias 'live' now points at version 7 (RESULTS_TABLE -> quarantine table)")

    # Step 3: dispatch each request through the (now faulty) alias.
    first_failure_at = None
    for req_id in request_ids:
        req = store.get_demo_request(req_id)
        payload = {"request_id": req_id, "document": req.document.model_dump()}
        result = gateway.invoke(app_config.function_name, app_config.alias_name, payload)
        status = RequestStatus.FAILED if result.function_error else RequestStatus.COMPLETED
        store.update_demo_request(req_id, lambda r, s=status: r.model_copy(update={"status": s, "attempts": r.attempts + 1, "updated_at": datetime.now(UTC)}))
        if result.function_error and first_failure_at is None:
            first_failure_at = datetime.now(UTC)
        log(f"  dispatched {req_id}: function_error={result.function_error!r}")

    # Step: normalize into an incident.
    incident = Incident(
        incident_id=new_id("inc"), workspace_id=app_config.workspace_id, app_id=app_config.app_id,
        demo_run_id=run.demo_run_id, fingerprint=f"{app_config.app_id}:v7", state=IncidentState.DETECTED,
        mode=Mode.FIXTURE, version=1, created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        impact=ImpactSummary(failed_requests=3, completed_requests=0, first_failure_at=first_failure_at, mode=Mode.FIXTURE),
    )
    store.create_incident(incident)
    log(f"opened incident {incident.incident_id} (3 failed requests)")

    incident = store.update_incident(incident.incident_id, incident.version, lambda i: i.model_copy(update={"state": IncidentState.INVESTIGATING, "version": i.version + 1, "updated_at": datetime.now(UTC)}))

    # Step 4: investigate (stub policy -- deterministic, no Bedrock needed).
    investigation_op_id = new_id("op")
    store.create_operation(Operation(operation_id=investigation_op_id, kind=OperationKind.INVESTIGATION, incident_id=incident.incident_id, status=OperationStatus.QUEUED, created_at=datetime.now(UTC), updated_at=datetime.now(UTC)))
    run_investigation(store, gateway, incident_id=incident.incident_id, operation_id=investigation_op_id, mode="fixture", data_dir=str(data_dir), agent_mode="stub")

    diagnosis = store.get_latest_diagnosis(incident.incident_id)
    log(f"diagnosis: assessment={diagnosis.assessment.value} recommended_action={diagnosis.recommended_action.value}")
    log(f"  summary: {diagnosis.summary}")

    incident = store.get_incident(incident.incident_id)
    if incident.state != IncidentState.AWAITING_APPROVAL:
        log(f"incident state is {incident.state.value}, not awaiting_approval -- stopping here (this is a valid abstention outcome).")
        return 0

    # Step 5: construct the plan via the deterministic policy.
    from mcp_server import tools as evidence_tools
    ctx = RunContext(incident_id=incident.incident_id, run_id=investigation_op_id, app_id=app_config.app_id, demo_run_id=run.demo_run_id, workspace_id=app_config.workspace_id, mode="fixture", data_dir=str(data_dir))
    diff_resp = evidence_tools.get_release_diff(ctx, store, gateway, incident.incident_id)
    diff_data = diff_resp["data"]
    known_good = store.get_known_good_deployment(app_config.app_id)

    diff_snapshot = ReleaseDiffSnapshot(
        alias_name=diff_data["alias_name"], current_version=diff_data["current_version"],
        known_good_version=diff_data["known_good_version"], alias_revision_id=diff_data["alias_revision_id"],
        weighted_routing=diff_data["weighted_routing"], schema_compatible=diff_data["schema_compatible"],
        changes=diff_data["changes"], processor_role_matches_manifest=diff_data["processor_role_matches_manifest"],
    )
    plan = evaluate(
        incident_id=incident.incident_id, app_config=app_config, diagnosis_recommended_action=diagnosis.recommended_action,
        diagnosis_evidence_ids=diagnosis.evidence_ids, diff=diff_snapshot, known_good_entry=known_good,
        replay_request_ids=[(rid, store.get_demo_request(rid).payload_sha256) for rid in request_ids],
    )
    from packages.domain.plan_policy import PlanPolicyRejection

    if isinstance(plan, PlanPolicyRejection):
        log(f"plan policy rejected: {plan.reason_code} -- {plan.message}")
        return 0

    digest = compute_plan_digest(plan)
    from packages.domain.plan import canonical_bytes

    store.put_plan(plan, canonical_bytes(plan), digest)
    log(f"plan {plan.plan_id} built: rollback {plan.from_version} -> {plan.to_version}, digest={digest[:24]}...")

    incident = store.update_incident(incident.incident_id, incident.version, lambda i: i.model_copy(update={"active_plan_id": plan.plan_id, "version": i.version + 1, "updated_at": datetime.now(UTC)}))

    # Step 6: operator approval.
    approval = Approval(approval_id=new_id("approval"), plan_id=plan.plan_id, plan_digest=digest, incident_id=incident.incident_id, actor_id="local-owner", decision=Decision.APPROVE, decided_at=datetime.now(UTC), plan_expires_at=plan.expires_at)
    store.put_approval(approval)
    log(f"approved by {approval.actor_id}")

    execution_op_id = new_id("op")
    store.create_operation(Operation(operation_id=execution_op_id, kind=OperationKind.EXECUTION, incident_id=incident.incident_id, status=OperationStatus.QUEUED, created_at=datetime.now(UTC), updated_at=datetime.now(UTC)))
    store.put_execution_record(ExecutionRecord(operation_id=execution_op_id, plan_id=plan.plan_id, approval_id=approval.approval_id, incident_id=incident.incident_id))
    incident = store.update_incident(incident.incident_id, incident.version, lambda i: i.model_copy(update={"active_operation_id": execution_op_id, "version": i.version + 1, "updated_at": datetime.now(UTC)}))

    # Step 7-9: executor applies, verifies, replays, and writes the receipt.
    run_execution(store, gateway, operation_id=execution_op_id)

    incident = store.get_incident(incident.incident_id)
    receipt = store.get_latest_receipt(incident.incident_id)
    log(f"final incident state: {incident.state.value}")
    if receipt:
        log(f"receipt {receipt.receipt_id}: outcome={receipt.outcome.value} applied_version={receipt.applied_version}")
        for check in receipt.verification:
            log(f"  check {check.check}: passed={check.passed} ({check.details})")
        for req in receipt.requests:
            log(f"  request {req.request_id}: {req.outcome.value}")
        log(f"  unresolved: {receipt.unresolved_request_ids}")
    else:
        log("no receipt was produced (execution did not reach receipt phase)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
