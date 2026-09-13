"""Every HTTP route, matching docs/specs/IncidentPilot-API-Contract.md.

Handlers are plain ``def`` (not ``async def``) wherever they touch the
synchronous store, so Starlette runs them in its thread pool instead of
blocking the event loop.

The rule this module exists to enforce: an HTTP request may *persist a
decision and enqueue work*, never perform the work. Approving a plan writes
an Approval plus an execution operation in one transaction and returns 202;
``services/executor`` independently re-validates everything immediately
before it touches AWS (build brief section 10).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from demo.workload.processor import canonicalize_input, payload_sha256
from packages.domain.enums import (
    Decision,
    ErrorCode,
    IncidentState,
    OperationKind,
    OperationStatus,
    RequestStatus,
)
from packages.domain.errors import ApiError
from packages.domain.events import EventType
from packages.domain.ids import new_id
from packages.domain.models import (
    Approval,
    DemoDocument,
    DemoRequest,
    DemoRun,
    ExecutionRecord,
    Incident,
    Operation,
)
from packages.domain.plan import verify_plan_digest
from packages.storage.interface import ConcurrencyConflict, ControlPlaneStore
from services.api import idempotency, schemas
from services.api.auth import Principal, authenticate
from services.api.config import Settings, get_gateway, get_settings, get_store
from services.intake import open_or_attach_incident, queue_investigation
from services.transitions import apply_transition

router = APIRouter()

#: Only these sources may open an incident (API contract: "Allow only
#: registered sources"). A caller cannot invent a source name.
REGISTERED_INCIDENT_SOURCES = frozenset({"incidentpilot.demo-dispatcher", "incidentpilot.eventbridge"})


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def _principal(request: Request) -> Principal:
    return authenticate(request, get_store(), get_settings())


def _store_dep() -> ControlPlaneStore:
    return get_store()


def _settings_dep() -> Settings:
    return get_settings()


def _require_incident(store: ControlPlaneStore, incident_id: str, principal: Principal) -> Incident:
    incident = store.get_incident(incident_id)
    # Authorize on workspace, never on "the ID was hard to guess"
    # (build brief section 9).
    if incident is None or incident.workspace_id != principal.workspace_id:
        raise ApiError(ErrorCode.NOT_FOUND, "incident not found")
    return incident


# --------------------------------------------------------------------------
# Health and capabilities
# --------------------------------------------------------------------------


@router.get("/healthz", tags=["meta"])
def healthz() -> dict[str, str]:
    """Process readiness only -- no configuration details, no auth."""
    return {"status": "ok"}


@router.get("/v1/capabilities", response_model=schemas.CapabilitiesResponse, tags=["meta"])
def capabilities(
    principal: Principal = Depends(_principal),
    store: ControlPlaneStore = Depends(_store_dep),
    settings: Settings = Depends(_settings_dep),
) -> schemas.CapabilitiesResponse:
    """What this deployment actually does -- not what it aspires to."""
    app_config = store.get_app_config(settings.app_id)
    return schemas.CapabilitiesResponse(
        mode=settings.mode,
        agent_mode=settings.agent_mode.value,
        features=schemas.Features(
            mcp_evidence=True,
            alias_rollback=True,
            bounded_replay=True,
            agentcore_runtime=False,
        ),
        applications=(
            [schemas.ApplicationSummary(app_id=app_config.app_id, label=app_config.label)] if app_config else []
        ),
    )


# --------------------------------------------------------------------------
# Demo runs and requests
# --------------------------------------------------------------------------


@router.post("/v1/demo/runs", status_code=201, response_model=schemas.CreateDemoRunResponse, tags=["demo"])
def create_demo_run(
    body: schemas.CreateDemoRunRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(_principal),
    store: ControlPlaneStore = Depends(_store_dep),
    settings: Settings = Depends(_settings_dep),
) -> Any:
    key = idempotency.require_key(idempotency_key)
    payload = body.model_dump()
    replay = idempotency.check_replay(store, key, principal, "POST", "/v1/demo/runs", payload)
    if replay:
        return JSONResponse(status_code=replay.status_code, content=replay.body)

    if store.get_app_config(body.app_id) is None:
        raise ApiError(ErrorCode.NOT_FOUND, "unknown app_id")

    run = DemoRun(
        demo_run_id=new_id("run"),
        workspace_id=principal.workspace_id,
        app_id=body.app_id,
        mode=settings.mode,
        created_at=datetime.now(UTC),
    )
    store.create_demo_run(run)
    response = schemas.CreateDemoRunResponse(
        demo_run_id=run.demo_run_id,
        app_id=run.app_id,
        created_at=_iso(run.created_at),
        mode=run.mode,
        max_requests=run.max_requests,
    ).model_dump(mode="json")
    idempotency.record(
        store,
        key,
        principal,
        "POST",
        "/v1/demo/runs",
        payload,
        resource_id=run.demo_run_id,
        response=response,
        status_code=201,
    )
    return JSONResponse(status_code=201, content=response)


@router.post("/v1/demo/requests", status_code=202, response_model=schemas.AcceptedResponse, tags=["demo"])
def submit_demo_request(
    body: schemas.CreateDemoRequestRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(_principal),
    store: ControlPlaneStore = Depends(_store_dep),
) -> Any:
    """Persist the request, then queue its processing attempt.

    Order matters: the input is durable *before* anything tries to process
    it, so a failed attempt leaves a recoverable request rather than a lost
    one (build brief section 5, step 3).
    """
    key = idempotency.require_key(idempotency_key)
    payload = body.model_dump()
    replay = idempotency.check_replay(store, key, principal, "POST", "/v1/demo/requests", payload)
    if replay:
        return JSONResponse(status_code=replay.status_code, content=replay.body)

    run = store.get_demo_run(body.demo_run_id)
    if run is None or run.workspace_id != principal.workspace_id:
        raise ApiError(ErrorCode.NOT_FOUND, "demo run not found")

    existing = store.list_demo_requests(run.demo_run_id)
    if len(existing) >= run.max_requests:
        raise ApiError(ErrorCode.VALIDATION_ERROR, f"this run already holds its maximum of {run.max_requests} requests")

    request_id = new_id("docreq")
    document = DemoDocument(**body.document.model_dump())
    digest = payload_sha256(canonicalize_input(request_id, document.model_dump()))
    now = datetime.now(UTC)
    store.create_demo_request(
        DemoRequest(
            request_id=request_id,
            demo_run_id=run.demo_run_id,
            app_id=run.app_id,
            document=document,
            payload_sha256=digest,
            status=RequestStatus.ACCEPTED,
            created_at=now,
            updated_at=now,
        )
    )

    operation_id = new_id("op")
    store.create_operation(
        Operation(
            operation_id=operation_id,
            kind=OperationKind.PROCESSING,
            incident_id=request_id,  # no incident yet; the request is the aggregate
            status=OperationStatus.QUEUED,
            created_at=now,
            updated_at=now,
        )
    )
    response = schemas.AcceptedResponse(
        resource_id=request_id,
        request_id=request_id,
        operation_id=operation_id,
        status_url=f"/v1/operations/{operation_id}",
    ).model_dump(mode="json")
    idempotency.record(
        store,
        key,
        principal,
        "POST",
        "/v1/demo/requests",
        payload,
        resource_id=request_id,
        response=response,
        status_code=202,
    )
    return JSONResponse(status_code=202, content=response)


@router.get("/v1/demo/requests/{request_id}", response_model=schemas.DemoRequestResponse, tags=["demo"])
def get_demo_request(
    request_id: str,
    principal: Principal = Depends(_principal),
    store: ControlPlaneStore = Depends(_store_dep),
) -> schemas.DemoRequestResponse:
    req = store.get_demo_request(request_id)
    if req is None:
        raise ApiError(ErrorCode.NOT_FOUND, "request not found")
    run = store.get_demo_run(req.demo_run_id)
    if run is None or run.workspace_id != principal.workspace_id:
        raise ApiError(ErrorCode.NOT_FOUND, "request not found")
    return schemas.DemoRequestResponse(
        request_id=req.request_id,
        demo_run_id=req.demo_run_id,
        status=req.status.value,
        payload_sha256=req.payload_sha256,
        attempts=req.attempts,
        last_error_code=req.last_error_code,
        result_id=req.result_id,
        created_at=_iso(req.created_at),
        updated_at=_iso(req.updated_at),
    )


# --------------------------------------------------------------------------
# Incident intake and investigations
# --------------------------------------------------------------------------


@router.post("/v1/incidents", status_code=202, response_model=schemas.AcceptedResponse, tags=["incidents"])
def open_incident(
    body: schemas.IncidentEventIn,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(_principal),
    store: ControlPlaneStore = Depends(_store_dep),
    settings: Settings = Depends(_settings_dep),
) -> Any:
    """Normalize and deduplicate one failure event into an incident.

    Two independent dedup layers: ``source_event_id`` stops exact
    redelivery, and an existing non-terminal incident for the same
    app/run/deployment fingerprint absorbs near-simultaneous failures
    instead of opening a second investigation.
    """
    key = idempotency.require_key(idempotency_key)
    payload = body.model_dump()
    replay = idempotency.check_replay(store, key, principal, "POST", "/v1/incidents", payload)
    if replay:
        return JSONResponse(status_code=replay.status_code, content=replay.body)

    if body.source not in REGISTERED_INCIDENT_SOURCES:
        raise ApiError(ErrorCode.FORBIDDEN, "unregistered event source")

    run = store.get_demo_run(body.demo_run_id)
    if run is None or run.workspace_id != principal.workspace_id or run.app_id != body.app_id:
        raise ApiError(ErrorCode.NOT_FOUND, "demo run not found for this application")

    # Confirm every referenced request really belongs to this run before
    # letting it shape an incident's impact numbers.
    run_request_ids = {r.request_id for r in store.list_demo_requests(run.demo_run_id)}
    unknown = set(body.request_ids) - run_request_ids
    if unknown:
        raise ApiError(ErrorCode.VALIDATION_ERROR, "request_ids do not belong to this demo run")

    incident_id, operation_id = open_or_attach_incident(
        store,
        get_gateway(),
        workspace_id=principal.workspace_id,
        app_id=body.app_id,
        demo_run_id=body.demo_run_id,
        source=body.source,
        source_event_id=body.source_event_id,
        failure_kind=body.failure_kind,
        request_ids=body.request_ids,
        observed_at=datetime.fromisoformat(body.observed_at.replace("Z", "+00:00")),
        mode=settings.mode,
    )

    response = schemas.AcceptedResponse(
        resource_id=incident_id,
        incident_id=incident_id,
        operation_id=operation_id or "",
        status_url=f"/v1/operations/{operation_id}" if operation_id else "",
    ).model_dump(mode="json")
    idempotency.record(
        store,
        key,
        principal,
        "POST",
        "/v1/incidents",
        payload,
        resource_id=incident_id,
        response=response,
        status_code=202,
    )
    return JSONResponse(status_code=202, content=response)


@router.post(
    "/v1/incidents/{incident_id}/investigations",
    status_code=202,
    response_model=schemas.AcceptedResponse,
    tags=["incidents"],
)
def start_investigation(
    incident_id: str,
    body: schemas.StartInvestigationRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(_principal),
    store: ControlPlaneStore = Depends(_store_dep),
) -> Any:
    """Queue a re-investigation. Invalidates any unexecuted plan."""
    key = idempotency.require_key(idempotency_key)
    payload = body.model_dump()
    replay = idempotency.check_replay(
        store, key, principal, "POST", f"/v1/incidents/{incident_id}/investigations", payload
    )
    if replay:
        return JSONResponse(status_code=replay.status_code, content=replay.body)

    incident = _require_incident(store, incident_id, principal)
    if incident.version != body.expected_incident_version:
        raise ApiError(
            ErrorCode.STALE_INCIDENT_VERSION,
            "the incident changed since you loaded it",
            details={"current_version": incident.version},
        )
    if incident.state in (
        IncidentState.INVESTIGATING,
        IncidentState.APPLYING,
        IncidentState.VERIFYING,
        IncidentState.REPLAYING,
    ):
        raise ApiError(ErrorCode.DECISION_CONFLICT, f"cannot re-investigate while {incident.state.value}")

    if incident.active_plan_id:
        store.append_event(
            incident_id,
            EventType.PLAN_INVALIDATED.value,
            {
                "plan_id": incident.active_plan_id,
                "reason": "a new investigation was requested",
            },
        )
        store.update_incident(
            incident_id,
            incident.version,
            lambda inc: inc.model_copy(
                update={"active_plan_id": None, "version": inc.version + 1, "updated_at": datetime.now(UTC)}
            ),
        )
        incident = store.get_incident(incident_id)

    operation_id = queue_investigation(store, incident.incident_id)
    response = schemas.AcceptedResponse(
        resource_id=incident_id,
        incident_id=incident_id,
        operation_id=operation_id,
        status_url=f"/v1/operations/{operation_id}",
    ).model_dump(mode="json")
    idempotency.record(
        store,
        key,
        principal,
        "POST",
        f"/v1/incidents/{incident_id}/investigations",
        payload,
        resource_id=incident_id,
        response=response,
        status_code=202,
    )
    return JSONResponse(status_code=202, content=response)


@router.get("/v1/incidents", response_model=schemas.IncidentListResponse, tags=["incidents"])
def list_incidents(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    principal: Principal = Depends(_principal),
    store: ControlPlaneStore = Depends(_store_dep),
) -> schemas.IncidentListResponse:
    page = store.list_incidents(cursor, limit)
    # Reauthorize every page; a cursor carries no authority of its own.
    visible = [i for i in page.items if i.workspace_id == principal.workspace_id]
    return schemas.IncidentListResponse(
        items=[
            schemas.IncidentListItem(
                incident_id=i.incident_id,
                app_id=i.app_id,
                state=i.state,
                mode=i.mode,
                failed_requests=i.impact.failed_requests,
                updated_at=_iso(i.updated_at),
            )
            for i in visible
        ],
        next_cursor=page.next_cursor,
    )


@router.get("/v1/incidents/{incident_id}", response_model=schemas.IncidentResponse, tags=["incidents"])
def get_incident(
    incident_id: str,
    principal: Principal = Depends(_principal),
    store: ControlPlaneStore = Depends(_store_dep),
) -> schemas.IncidentResponse:
    incident = _require_incident(store, incident_id, principal)
    diagnosis = store.get_latest_diagnosis(incident_id)
    return schemas.IncidentResponse(
        incident_id=incident.incident_id,
        version=incident.version,
        app_id=incident.app_id,
        demo_run_id=incident.demo_run_id,
        state=incident.state,
        mode=incident.mode,
        impact=schemas.ImpactOut(
            failed_requests=incident.impact.failed_requests,
            completed_requests=incident.impact.completed_requests,
            first_failure_at=_iso(incident.impact.first_failure_at),
            service=incident.impact.service,
        ),
        diagnosis=(
            schemas.DiagnosisSummary(
                assessment=diagnosis.assessment,
                summary=diagnosis.summary,
                evidence_ids=diagnosis.evidence_ids,
                # Never trim unknowns to tidy the UI (API contract, section 3).
                unknowns=diagnosis.unknowns,
                recommended_action=diagnosis.recommended_action.value,
                hypotheses=[h.model_dump(mode="json") for h in diagnosis.hypotheses],
                agent_mode=diagnosis.agent_mode.value,
                model_id=diagnosis.model_id,
                tool_call_count=diagnosis.tool_call_count,
                duration_ms=diagnosis.duration_ms,
            )
            if diagnosis
            else None
        ),
        active_plan_id=incident.active_plan_id,
        active_operation_id=incident.active_operation_id,
        latest_receipt_id=incident.latest_receipt_id,
        created_at=_iso(incident.created_at),
        updated_at=_iso(incident.updated_at),
    )


# --------------------------------------------------------------------------
# Events and evidence
# --------------------------------------------------------------------------


@router.get("/v1/incidents/{incident_id}/events", response_model=schemas.EventsResponse, tags=["incidents"])
def list_events(
    incident_id: str,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=100),
    principal: Principal = Depends(_principal),
    store: ControlPlaneStore = Depends(_store_dep),
) -> schemas.EventsResponse:
    _require_incident(store, incident_id, principal)
    page = store.list_events(incident_id, after_sequence, limit)
    return schemas.EventsResponse(
        items=[
            schemas.EventOut(
                sequence=e.sequence,
                event_id=e.event_id,
                type=e.type,
                occurred_at=_iso(e.occurred_at),
                data=e.data,
            )
            for e in page.items
        ],
        last_sequence=page.last_sequence,
        has_more=page.has_more,
    )


@router.get(
    "/v1/incidents/{incident_id}/evidence/{evidence_id}",
    response_model=schemas.EvidenceResponse,
    tags=["incidents"],
)
def get_evidence(
    incident_id: str,
    evidence_id: str,
    principal: Principal = Depends(_principal),
    store: ControlPlaneStore = Depends(_store_dep),
) -> schemas.EvidenceResponse:
    _require_incident(store, incident_id, principal)
    evidence = store.get_evidence(incident_id, evidence_id)
    if evidence is None:
        raise ApiError(ErrorCode.NOT_FOUND, "evidence not found for this incident")
    return schemas.EvidenceResponse(
        evidence_id=evidence.evidence_id,
        incident_id=evidence.incident_id,
        run_id=evidence.run_id,
        source_type=evidence.source_type.value,
        source_ref=evidence.source_ref,
        observed_at=_iso(evidence.observed_at),
        retrieved_at=_iso(evidence.retrieved_at),
        payload_sha256=evidence.payload_sha256,
        payload=evidence.payload,
        truncated=evidence.truncated,
        warnings=[],
    )


# --------------------------------------------------------------------------
# Plan and decision
# --------------------------------------------------------------------------


@router.get("/v1/incidents/{incident_id}/plans/{plan_id}", response_model=schemas.PlanResponse, tags=["recovery"])
def get_plan(
    incident_id: str,
    plan_id: str,
    principal: Principal = Depends(_principal),
    store: ControlPlaneStore = Depends(_store_dep),
) -> schemas.PlanResponse:
    _require_incident(store, incident_id, principal)
    plan = store.get_plan(plan_id)
    if plan is None or plan.incident_id != incident_id:
        raise ApiError(ErrorCode.NOT_FOUND, "plan not found for this incident")
    digest = store.get_plan_digest(plan_id)
    replay_count = len(plan.replay_requests)
    confirmation = (
        f"Approve rollback to version {plan.to_version} and replay these {replay_count} requests."
        if replay_count
        else f"Approve rollback to version {plan.to_version}. No requests will be replayed."
    )
    return schemas.PlanResponse(
        plan=plan.model_dump(mode="json"),
        plan_digest=digest,
        presentation={
            "confirmation": confirmation,
            "action": f"Point alias '{plan.alias_name}' from version {plan.from_version} back to version {plan.to_version}.",
            "expires_at": _iso(plan.expires_at),
            "affected_request_ids": [r.request_id for r in plan.replay_requests],
        },
    )


@router.post("/v1/incidents/{incident_id}/decisions", tags=["recovery"])
def decide(
    incident_id: str,
    body: schemas.DecisionRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(_principal),
    store: ControlPlaneStore = Depends(_store_dep),
) -> Any:
    """Approve or reject a plan.

    On approval this writes the Approval *and* the execution operation
    together, then returns 202. It does not touch AWS: the executor
    re-validates the digest, expiry, and the live alias revision immediately
    before mutating (build brief section 10).
    """
    key = idempotency.require_key(idempotency_key)
    payload = body.model_dump(mode="json")
    route = f"/v1/incidents/{incident_id}/decisions"
    replay = idempotency.check_replay(store, key, principal, "POST", route, payload)
    if replay:
        return JSONResponse(status_code=replay.status_code, content=replay.body)

    incident = _require_incident(store, incident_id, principal)
    if incident.version != body.expected_incident_version:
        raise ApiError(
            ErrorCode.STALE_INCIDENT_VERSION,
            "the incident changed since you loaded it; reload before deciding",
            details={"current_version": incident.version},
        )
    if incident.state is not IncidentState.AWAITING_APPROVAL:
        raise ApiError(ErrorCode.DECISION_CONFLICT, f"incident is {incident.state.value}, not awaiting approval")

    plan = store.get_plan(body.plan_id)
    if plan is None or plan.incident_id != incident_id:
        raise ApiError(ErrorCode.NOT_FOUND, "plan not found for this incident")
    if incident.active_plan_id != body.plan_id:
        raise ApiError(ErrorCode.STALE_PLAN, "this plan was superseded by a newer investigation")
    if store.get_approval_for_plan(body.plan_id) is not None:
        raise ApiError(ErrorCode.DECISION_CONFLICT, "this plan already has a decision")

    stored_digest = store.get_plan_digest(body.plan_id)
    if stored_digest != body.plan_digest or not verify_plan_digest(plan, body.plan_digest):
        raise ApiError(
            ErrorCode.PLAN_DIGEST_MISMATCH,
            "the approved plan does not match the stored plan; reload and review it again",
        )
    now = datetime.now(UTC)
    if now > plan.expires_at:
        raise ApiError(ErrorCode.PLAN_EXPIRED, "this plan expired; run a new investigation")

    approval = Approval(
        approval_id=new_id("approval"),
        plan_id=plan.plan_id,
        plan_digest=stored_digest,
        incident_id=incident_id,
        # The actor comes from the verified identity, never from the body.
        actor_id=principal.actor_id,
        decision=body.decision,
        decided_at=now,
        plan_expires_at=plan.expires_at,
    )
    try:
        store.put_approval(approval)
    except ConcurrencyConflict as exc:
        raise ApiError(ErrorCode.DECISION_CONFLICT, "this plan already has a decision") from exc

    store.append_event(
        incident_id,
        EventType.DECISION_RECORDED.value,
        {
            "approval_id": approval.approval_id,
            "plan_id": plan.plan_id,
            "decision": body.decision.value,
            "actor_id": principal.actor_id,
        },
    )

    if body.decision is Decision.REJECT:
        apply_transition(store, incident_id, IncidentState.REJECTED)
        response = schemas.RejectionResponse(
            approval_id=approval.approval_id,
            incident_id=incident_id,
            state=IncidentState.REJECTED,
            decision="reject",
        ).model_dump(mode="json")
        idempotency.record(
            store, key, principal, "POST", route, payload, resource_id=incident_id, response=response, status_code=200
        )
        return JSONResponse(status_code=200, content=response)

    operation_id = new_id("op")
    store.create_operation(
        Operation(
            operation_id=operation_id,
            kind=OperationKind.EXECUTION,
            incident_id=incident_id,
            status=OperationStatus.QUEUED,
            created_at=now,
            updated_at=now,
        )
    )
    store.put_execution_record(
        ExecutionRecord(
            operation_id=operation_id,
            plan_id=plan.plan_id,
            approval_id=approval.approval_id,
            incident_id=incident_id,
        )
    )
    current = store.get_incident(incident_id)
    store.update_incident(
        incident_id,
        current.version,
        lambda inc: inc.model_copy(
            update={"active_operation_id": operation_id, "version": inc.version + 1, "updated_at": now}
        ),
    )

    response = schemas.AcceptedResponse(
        resource_id=incident_id,
        incident_id=incident_id,
        approval_id=approval.approval_id,
        operation_id=operation_id,
        status_url=f"/v1/operations/{operation_id}",
    ).model_dump(mode="json")
    idempotency.record(
        store, key, principal, "POST", route, payload, resource_id=incident_id, response=response, status_code=202
    )
    return JSONResponse(status_code=202, content=response)


# --------------------------------------------------------------------------
# Operations and receipt
# --------------------------------------------------------------------------


@router.get("/v1/operations/{operation_id}", response_model=schemas.OperationResponse, tags=["recovery"])
def get_operation(
    operation_id: str,
    principal: Principal = Depends(_principal),
    store: ControlPlaneStore = Depends(_store_dep),
) -> schemas.OperationResponse:
    operation = store.get_operation(operation_id)
    if operation is None:
        raise ApiError(ErrorCode.NOT_FOUND, "operation not found")
    return schemas.OperationResponse(
        operation_id=operation.operation_id,
        kind=operation.kind.value,
        incident_id=operation.incident_id,
        status=operation.status,
        phase=operation.phase,
        created_at=_iso(operation.created_at),
        updated_at=_iso(operation.updated_at),
        started_at=_iso(operation.started_at),
        finished_at=_iso(operation.finished_at),
        error=operation.error,
        result=operation.result,
    )


@router.get("/v1/incidents/{incident_id}/receipt", tags=["recovery"])
def get_receipt(
    incident_id: str,
    format: str = Query(default="json", pattern="^(json|markdown)$"),
    principal: Principal = Depends(_principal),
    store: ControlPlaneStore = Depends(_store_dep),
) -> Response:
    _require_incident(store, incident_id, principal)
    receipt = store.get_latest_receipt(incident_id)
    if receipt is None:
        raise ApiError(ErrorCode.RECEIPT_NOT_READY, "no receipt exists for this incident yet")

    if format == "markdown":
        from services.api.receipt_markdown import render_markdown

        return PlainTextResponse(
            content=render_markdown(receipt),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="receipt-{receipt.receipt_id}.md"'},
        )
    return JSONResponse(status_code=200, content=receipt.model_dump(mode="json"))
