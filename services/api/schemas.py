"""HTTP request/response models.

Request models set ``extra="forbid"`` so an unknown field is a 422 rather
than a silently ignored typo (API contract: "Validate JSON with unknown
fields rejected"). Note what these models deliberately do NOT accept
anywhere: ARNs, role names, table names, shell commands, URLs, log queries.
Every AWS identifier the system acts on comes from trusted configuration.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from packages.domain.enums import (
    Assessment,
    Decision,
    IncidentState,
    Mode,
    OperationPhase,
    OperationStatus,
)


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- capabilities ----------------------------------------------------------


class Features(_Response):
    mcp_evidence: bool
    alias_rollback: bool
    bounded_replay: bool
    agentcore_runtime: bool


class ApplicationSummary(_Response):
    app_id: str
    label: str


class CapabilitiesResponse(_Response):
    schema_version: Literal["1.0"] = "1.0"
    mode: Mode
    agent_mode: str
    features: Features
    applications: list[ApplicationSummary]


# -- demo runs and requests ------------------------------------------------


class CreateDemoRunRequest(_Request):
    app_id: str


class CreateDemoRunResponse(_Response):
    demo_run_id: str
    app_id: str
    created_at: str
    mode: Mode
    max_requests: int


class DemoDocumentIn(_Request):
    fixture_id: Literal[
        "invoice-example-a", "invoice-example-b", "invoice-example-c", "contract-example-a", "report-example-a"
    ]
    document_type: Literal["invoice", "contract", "receipt", "report"]
    page_count: int = Field(ge=1, le=10)


class CreateDemoRequestRequest(_Request):
    demo_run_id: str
    document: DemoDocumentIn


class AcceptedResponse(_Response):
    """The common 202 envelope (API contract, "Shared conventions")."""

    resource_id: str
    operation_id: str
    status: Literal["accepted"] = "accepted"
    status_url: str
    request_id: str | None = None
    incident_id: str | None = None
    approval_id: str | None = None


class DemoRequestResponse(_Response):
    request_id: str
    demo_run_id: str
    status: str
    payload_sha256: str
    attempts: int
    last_error_code: str | None
    result_id: str | None
    created_at: str
    updated_at: str


# -- incidents -------------------------------------------------------------


class IncidentEventIn(_Request):
    source_event_id: str = Field(min_length=1, max_length=128)
    source: str
    app_id: str
    demo_run_id: str
    observed_at: str
    failure_kind: Literal["processor_invocation_failed"]
    request_ids: list[str] = Field(max_length=10)
    evidence_ref: str | None = None


class StartInvestigationRequest(_Request):
    expected_incident_version: int = Field(ge=1)


class DiagnosisSummary(_Response):
    assessment: Assessment
    summary: str
    evidence_ids: list[str]
    unknowns: list[str]
    recommended_action: str
    hypotheses: list[dict[str, Any]]
    agent_mode: str
    model_id: str
    tool_call_count: int
    duration_ms: int


class ImpactOut(_Response):
    failed_requests: int
    completed_requests: int
    first_failure_at: str | None
    service: str


class IncidentResponse(_Response):
    incident_id: str
    version: int
    app_id: str
    demo_run_id: str
    state: IncidentState
    mode: Mode
    impact: ImpactOut
    diagnosis: DiagnosisSummary | None
    active_plan_id: str | None
    active_operation_id: str | None
    latest_receipt_id: str | None
    created_at: str
    updated_at: str


class IncidentListItem(_Response):
    incident_id: str
    app_id: str
    state: IncidentState
    mode: Mode
    failed_requests: int
    updated_at: str


class IncidentListResponse(_Response):
    items: list[IncidentListItem]
    next_cursor: str | None


# -- events / evidence -----------------------------------------------------


class EventOut(_Response):
    sequence: int
    event_id: str
    type: str
    occurred_at: str
    data: dict[str, Any]


class EventsResponse(_Response):
    items: list[EventOut]
    last_sequence: int
    has_more: bool


class EvidenceResponse(_Response):
    evidence_id: str
    incident_id: str
    run_id: str
    source_type: str
    source_ref: str
    observed_at: str
    retrieved_at: str
    payload_sha256: str
    payload: dict[str, Any]
    truncated: bool
    warnings: list[str]


# -- plans and decisions ---------------------------------------------------


class PlanResponse(_Response):
    plan: dict[str, Any]
    plan_digest: str
    presentation: dict[str, Any]


class DecisionRequest(_Request):
    plan_id: str
    plan_digest: str
    expected_incident_version: int = Field(ge=1)
    decision: Decision


class RejectionResponse(_Response):
    approval_id: str
    incident_id: str
    state: IncidentState
    decision: Literal["reject"]


# -- operations ------------------------------------------------------------


class OperationResponse(_Response):
    operation_id: str
    kind: str
    incident_id: str
    status: OperationStatus
    phase: OperationPhase | None
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None
    result: dict[str, Any] | None
