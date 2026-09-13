"""Core domain entities.

These are storage- and transport-agnostic Pydantic models. `packages/storage`
adapters serialize them; `services/api` composes them into HTTP responses;
`agent` and `mcp_server` build `Evidence`/`Diagnosis` from evidence tool
calls. No entity here knows about SQLite, DynamoDB, or FastAPI.

Field sets follow docs/specs/IncidentPilot-Claude-Build-Brief.md section 9
("State and data contracts"), extended only where the API contract
(docs/specs/IncidentPilot-API-Contract.md) requires a concrete shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from packages.domain.enums import (
    AgentMode,
    Assessment,
    Decision,
    IncidentState,
    Mode,
    OperationKind,
    OperationPhase,
    OperationStatus,
    ReceiptOutcome,
    ReceiptRequestOutcome,
    RecommendedAction,
    RequestStatus,
    SourceType,
)
from packages.domain.types import NonEmptyStr, OpaqueId, Sha256Digest, Sha256Hex


class Entity(BaseModel):
    """Base for every domain entity: forbid silent typos in stored JSON."""

    model_config = ConfigDict(extra="forbid", frozen=False)


# --------------------------------------------------------------------------
# Impact / evidence / diagnosis
# --------------------------------------------------------------------------


class ImpactSummary(Entity):
    failed_requests: int = Field(ge=0, le=10)
    completed_requests: int = Field(ge=0, le=10)
    first_failure_at: datetime | None = None
    service: str = "document-request-processor"
    mode: Mode


class Evidence(Entity):
    """A single stored, sanitized source snapshot.

    ``payload`` is the full sanitized content (server-side only); MCP tool
    responses and the incident evidence panel both derive their bounded
    ``summary``/``payload_sha256`` from this record, never the reverse.
    """

    evidence_id: OpaqueId
    incident_id: OpaqueId
    run_id: OpaqueId
    source_type: SourceType
    tool_name: str
    source_ref: NonEmptyStr
    observed_at: datetime
    retrieved_at: datetime
    payload: dict[str, Any]
    payload_sha256: Sha256Hex
    summary: str = Field(max_length=2000)
    truncated: bool = False


class Hypothesis(Entity):
    description: str
    supporting_evidence_ids: list[OpaqueId] = Field(default_factory=list)
    contradicting_evidence_ids: list[OpaqueId] = Field(default_factory=list)


class Diagnosis(Entity):
    """The agent's structured output, exactly as returned by Strands.

    This is advisory. It cannot itself authorize a mutation -- see
    ``packages/domain/plan.py`` and docs/decisions/0002-read-only-mcp.md.
    """

    diagnosis_id: OpaqueId
    incident_id: OpaqueId
    run_id: OpaqueId
    assessment: Assessment
    summary: str
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    evidence_ids: list[OpaqueId] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    recommended_action: RecommendedAction
    agent_mode: AgentMode
    model_id: str
    prompt_version: str
    tool_call_count: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    created_at: datetime


# --------------------------------------------------------------------------
# Incident
# --------------------------------------------------------------------------


class Incident(Entity):
    incident_id: OpaqueId
    workspace_id: OpaqueId
    app_id: OpaqueId
    demo_run_id: OpaqueId
    fingerprint: str
    state: IncidentState
    mode: Mode
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    impact: ImpactSummary
    latest_diagnosis: Diagnosis | None = None
    active_plan_id: OpaqueId | None = None
    active_operation_id: OpaqueId | None = None
    latest_receipt_id: OpaqueId | None = None


# --------------------------------------------------------------------------
# Approval / execution
# --------------------------------------------------------------------------


class Approval(Entity):
    approval_id: OpaqueId
    plan_id: OpaqueId
    plan_digest: Sha256Digest
    incident_id: OpaqueId
    actor_id: str
    decision: Decision
    decided_at: datetime
    plan_expires_at: datetime


class VerificationCheck(Entity):
    check: str
    passed: bool
    observed_at: datetime
    evidence_id: OpaqueId | None = None
    details: str | None = None


class RequestExecutionOutcome(Entity):
    request_id: OpaqueId
    outcome: ReceiptRequestOutcome
    result_id: OpaqueId | None = None
    details: str | None = None


class ExecutionRecord(Entity):
    """Executor-internal state for one approved recovery operation.

    Not returned verbatim over HTTP; ``services/api`` projects the fields a
    client needs into the generic ``Operation``/``Receipt`` shapes. Kept
    separate from ``Operation`` because leasing/claim fields are an executor
    implementation detail, not a public contract.
    """

    operation_id: OpaqueId
    plan_id: OpaqueId
    approval_id: OpaqueId
    incident_id: OpaqueId
    lease_owner: str | None = None
    lease_version: int = Field(default=0, ge=0)
    lease_expires_at: datetime | None = None
    replay_deadline: datetime | None = None
    aws_request_ids: list[str] = Field(default_factory=list)
    actual_alias_revision_id: str | None = None
    applied_version: str | None = None
    verification: list[VerificationCheck] = Field(default_factory=list)
    per_request: list[RequestExecutionOutcome] = Field(default_factory=list)


class Operation(Entity):
    """Generic pollable unit of async work (`GET /v1/operations/{id}`)."""

    operation_id: OpaqueId
    kind: OperationKind
    incident_id: OpaqueId
    status: OperationStatus
    phase: OperationPhase | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    result: dict[str, Any] | None = None


# --------------------------------------------------------------------------
# Demo workload (fixture requests processed by the good/bad Lambda)
# --------------------------------------------------------------------------


class DemoRun(Entity):
    demo_run_id: OpaqueId
    workspace_id: OpaqueId
    app_id: OpaqueId
    mode: Mode
    max_requests: int = 10
    created_at: datetime
    locked_by_operation_id: OpaqueId | None = None


class DemoDocument(Entity):
    fixture_id: str
    document_type: Literal["invoice", "contract", "receipt", "report"]
    page_count: int = Field(ge=1, le=10)


class DemoRequest(Entity):
    request_id: OpaqueId
    demo_run_id: OpaqueId
    app_id: OpaqueId
    document: DemoDocument
    payload_sha256: Sha256Hex
    status: RequestStatus
    attempts: int = 0
    last_error_code: str | None = None
    result_id: OpaqueId | None = None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------
# Receipt
# --------------------------------------------------------------------------


class UsageInfo(Entity):
    model_id: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None


class Receipt(Entity):
    schema_version: Literal["1.0"] = "1.0"
    receipt_id: OpaqueId
    incident_id: OpaqueId
    mode: Mode
    outcome: ReceiptOutcome
    plan_id: OpaqueId
    approval_id: OpaqueId
    execution_id: OpaqueId
    applied_version: str | None
    verification: list[VerificationCheck]
    requests: list[RequestExecutionOutcome]
    unresolved_request_ids: list[OpaqueId]
    timing_ms: dict[str, int]
    usage: UsageInfo
    created_at: datetime


# --------------------------------------------------------------------------
# Event / outbox
# --------------------------------------------------------------------------


class OutboxEvent(Entity):
    event_id: OpaqueId
    aggregate_id: OpaqueId
    sequence: int = Field(ge=1)
    type: str
    occurred_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)
    delivered: bool = False
