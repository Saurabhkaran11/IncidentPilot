"""``ControlPlaneStore``: the one interface every adapter implements.

Deliberately synchronous. SQLite (fixture/local-live) and boto3/DynamoDB
(hosted-live) are both fundamentally synchronous; FastAPI route handlers
that touch the store are plain ``def`` functions so Starlette runs them in
its worker thread pool instead of blocking the event loop -- see
services/api/deps.py.

Every mutating method that matters for correctness takes an explicit
concurrency guard (``expected_version``, a conditional lease, or a
compare-and-swap on natural state) and raises ``ConcurrencyConflict``
instead of silently overwriting -- see build brief section 9 ("integer
record version for optimistic concurrency") and section 10 (leases/
conditional writes for the executor).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from packages.domain.config import AppConfig, DeploymentManifestEntry
from packages.domain.models import (
    Approval,
    DemoRequest,
    DemoRun,
    Diagnosis,
    Evidence,
    ExecutionRecord,
    Incident,
    Operation,
    OutboxEvent,
    Receipt,
)
from packages.domain.plan import RecoveryPlan


class ConcurrencyConflict(Exception):
    """Raised on a failed optimistic-concurrency or conditional write."""


class NotFound(Exception):
    """Raised when a required record does not exist."""


@dataclass(frozen=True)
class IdempotencyRecord:
    scope_key: str
    request_hash: str
    resource_id: str
    response_json: str
    status_code: int


@dataclass(frozen=True)
class EventPage:
    items: list[OutboxEvent]
    last_sequence: int
    has_more: bool


@dataclass(frozen=True)
class IncidentPage:
    items: list[Incident]
    next_cursor: str | None


@dataclass(frozen=True)
class OperatorTokenRecord:
    token_hash: str
    actor_id: str
    workspace_id: str
    aws_identity_arn: str
    issued_at: str
    expires_at: str
    boot_session_id: str


class ControlPlaneStore(ABC):
    # -- app / deployment configuration (trusted, not user-writable via API) --
    @abstractmethod
    def get_app_config(self, app_id: str) -> AppConfig | None: ...

    @abstractmethod
    def put_app_config(self, config: AppConfig) -> None: ...

    @abstractmethod
    def record_deployment_version(self, entry: DeploymentManifestEntry) -> None: ...

    @abstractmethod
    def get_deployment_version(self, app_id: str, version: str) -> DeploymentManifestEntry | None: ...

    @abstractmethod
    def get_known_good_deployment(self, app_id: str) -> DeploymentManifestEntry | None: ...

    # -- demo runs / requests (control-plane inbox, not the results table) --
    @abstractmethod
    def create_demo_run(self, run: DemoRun) -> None: ...

    @abstractmethod
    def get_demo_run(self, demo_run_id: str) -> DemoRun | None: ...

    @abstractmethod
    def try_lock_run(self, demo_run_id: str, operation_id: str) -> bool:
        """Claim the run's live-mutation lock. False if already held."""

    @abstractmethod
    def release_run_lock(self, demo_run_id: str, operation_id: str) -> None: ...

    @abstractmethod
    def create_demo_request(self, request: DemoRequest) -> None: ...

    @abstractmethod
    def get_demo_request(self, request_id: str) -> DemoRequest | None: ...

    @abstractmethod
    def list_demo_requests(self, demo_run_id: str) -> list[DemoRequest]: ...

    @abstractmethod
    def update_demo_request(self, request_id: str, mutator: Callable[[DemoRequest], DemoRequest]) -> DemoRequest:
        """Read-modify-write inside a transaction; ``mutator`` must be pure."""

    # -- incidents --
    @abstractmethod
    def create_incident(self, incident: Incident) -> None: ...

    @abstractmethod
    def get_incident(self, incident_id: str) -> Incident | None: ...

    @abstractmethod
    def find_active_incident(self, app_id: str, demo_run_id: str, fingerprint: str) -> Incident | None:
        """Non-terminal incident for the same app/run/deployment fingerprint."""

    @abstractmethod
    def update_incident(
        self, incident_id: str, expected_version: int, mutator: Callable[[Incident], Incident]
    ) -> Incident:
        """Raises ``ConcurrencyConflict`` if the stored version != expected_version."""

    @abstractmethod
    def list_incidents(self, cursor: str | None, limit: int) -> IncidentPage: ...

    # -- evidence / diagnosis --
    @abstractmethod
    def put_evidence(self, evidence: Evidence) -> None: ...

    @abstractmethod
    def get_evidence(self, incident_id: str, evidence_id: str) -> Evidence | None: ...

    @abstractmethod
    def list_evidence_for_run(self, run_id: str) -> list[Evidence]: ...

    @abstractmethod
    def put_diagnosis(self, diagnosis: Diagnosis) -> None: ...

    @abstractmethod
    def get_latest_diagnosis(self, incident_id: str) -> Diagnosis | None: ...

    # -- plans / approvals --
    @abstractmethod
    def put_plan(self, plan: RecoveryPlan, canonical_bytes: bytes, digest: str) -> None: ...

    @abstractmethod
    def get_plan(self, plan_id: str) -> RecoveryPlan | None: ...

    @abstractmethod
    def get_plan_digest(self, plan_id: str) -> str | None: ...

    @abstractmethod
    def put_approval(self, approval: Approval) -> None:
        """Raises ``ConcurrencyConflict`` if an approval already exists for the plan."""

    @abstractmethod
    def get_approval(self, approval_id: str) -> Approval | None: ...

    @abstractmethod
    def get_approval_for_plan(self, plan_id: str) -> Approval | None: ...

    # -- operations (generic pollable units) + execution records --
    @abstractmethod
    def create_operation(self, operation: Operation) -> None: ...

    @abstractmethod
    def get_operation(self, operation_id: str) -> Operation | None: ...

    @abstractmethod
    def update_operation(self, operation_id: str, mutator: Callable[[Operation], Operation]) -> Operation: ...

    @abstractmethod
    def list_operations_by_status(self, kind: str, status: str, limit: int) -> list[Operation]:
        """Fixture/local-live queue scan. The hosted deployment uses SQS
        message delivery instead and never calls this."""

    @abstractmethod
    def put_execution_record(self, record: ExecutionRecord) -> None: ...

    @abstractmethod
    def get_execution_record(self, operation_id: str) -> ExecutionRecord | None: ...

    @abstractmethod
    def update_execution_record(
        self, operation_id: str, mutator: Callable[[ExecutionRecord], ExecutionRecord]
    ) -> ExecutionRecord: ...

    @abstractmethod
    def claim_execution_lease(self, operation_id: str, owner: str, lease_seconds: int) -> bool:
        """Conditional claim: succeeds only if unclaimed or the prior lease expired."""

    # -- receipts --
    @abstractmethod
    def put_receipt(self, receipt: Receipt) -> None: ...

    @abstractmethod
    def get_latest_receipt(self, incident_id: str) -> Receipt | None: ...

    # -- events / activity timeline (outbox) --
    @abstractmethod
    def append_event(self, incident_id: str, type_: str, data: dict) -> OutboxEvent:
        """Assigns the next per-incident sequence number atomically."""

    @abstractmethod
    def list_events(self, incident_id: str, after_sequence: int, limit: int) -> EventPage: ...

    # -- idempotency (mutating POST routes) --
    @abstractmethod
    def get_idempotency_record(self, scope_key: str) -> IdempotencyRecord | None: ...

    @abstractmethod
    def put_idempotency_record(self, record: IdempotencyRecord) -> None:
        """Raises ``ConcurrencyConflict`` if the key exists with a different request_hash."""

    # -- source-event dedup (POST /v1/incidents) --
    @abstractmethod
    def seen_source_event(self, source: str, source_event_id: str) -> str | None:
        """Return the incident_id this source_event_id already produced, if any."""

    @abstractmethod
    def record_source_event(self, source: str, source_event_id: str, incident_id: str) -> None: ...

    # -- operator tokens (P0 local-live auth) --
    @abstractmethod
    def put_operator_token(self, record: OperatorTokenRecord) -> None: ...

    @abstractmethod
    def get_operator_token(self, token_hash: str) -> OperatorTokenRecord | None: ...

    @abstractmethod
    def revoke_all_operator_tokens(self) -> None:
        """Called on server boot: a restart invalidates all prior tokens."""
