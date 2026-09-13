"""Every closed vocabulary used across the domain, in one place.

Keeping enums here (instead of scattered per-model) is what lets
``state_machine.py`` and the contract tests assert exhaustiveness.
"""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    FIXTURE = "fixture"
    AWS_LIVE = "aws_live"


class AgentMode(StrEnum):
    BEDROCK = "bedrock"
    STUB = "stub"


class IncidentState(StrEnum):
    DETECTED = "detected"
    INVESTIGATING = "investigating"
    NEEDS_INFORMATION = "needs_information"
    AWAITING_APPROVAL = "awaiting_approval"
    REJECTED = "rejected"
    APPLYING = "applying"
    VERIFYING = "verifying"
    REPLAYING = "replaying"
    RECOVERED = "recovered"
    SERVICE_RESTORED_PENDING_REQUESTS = "service_restored_pending_requests"
    NEEDS_ATTENTION = "needs_attention"


#: Terminal states: no further application-initiated transition leaves them
#: except by starting a brand-new investigation (which is a fresh incident
#: lifecycle, not a transition out of the terminal state).
TERMINAL_INCIDENT_STATES = frozenset(
    {
        IncidentState.REJECTED,
        IncidentState.RECOVERED,
        IncidentState.SERVICE_RESTORED_PENDING_REQUESTS,
        IncidentState.NEEDS_ATTENTION,
    }
)


class RequestStatus(StrEnum):
    ACCEPTED = "accepted"
    PROCESSING = "processing"
    FAILED = "failed"
    COMPLETED = "completed"
    NEEDS_ATTENTION = "needs_attention"


class Assessment(StrEnum):
    SUPPORTED = "supported"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class RecommendedAction(StrEnum):
    ROLLBACK_ALIAS = "rollback_alias"
    NO_ACTION = "no_action"


class SourceType(StrEnum):
    CLOUDWATCH_LOG = "cloudwatch_log"
    LAMBDA_CONFIGURATION = "lambda_configuration"
    DEPLOYMENT_MANIFEST = "deployment_manifest"
    REQUEST_INBOX = "request_inbox"
    RUNBOOK = "runbook"


class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class OperationPhase(StrEnum):
    VALIDATE = "validate"
    APPLY = "apply"
    VERIFY = "verify"
    REPLAY = "replay"
    RECEIPT = "receipt"


class OperationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_ATTENTION = "needs_attention"


class OperationKind(StrEnum):
    PROCESSING = "processing"  # dispatch one demo request through the alias
    INVESTIGATION = "investigation"
    EXECUTION = "execution"


class ReceiptOutcome(StrEnum):
    RECOVERED = "recovered"
    PARTIAL = "partial"
    NEEDS_ATTENTION = "needs_attention"


class ReceiptRequestOutcome(StrEnum):
    COMPLETED = "completed"
    ALREADY_COMPLETED = "already_completed"
    STILL_FAILED = "still_failed"
    NOT_REPLAYED = "not_replayed"
    CONFLICT = "conflict"


class ErrorCode(StrEnum):
    """API-level error codes (the common error envelope's ``code`` field)."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    STALE_INCIDENT_VERSION = "STALE_INCIDENT_VERSION"
    STALE_PLAN = "STALE_PLAN"
    PLAN_EXPIRED = "PLAN_EXPIRED"
    PLAN_DIGEST_MISMATCH = "PLAN_DIGEST_MISMATCH"
    DECISION_CONFLICT = "DECISION_CONFLICT"
    RUN_LOCKED = "RUN_LOCKED"
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    RECEIPT_NOT_READY = "RECEIPT_NOT_READY"
    RATE_LIMITED = "RATE_LIMITED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class McpErrorCode(StrEnum):
    """Error codes used inside MCP tool output envelopes (contracts/mcp)."""

    RESOURCE_OUT_OF_SCOPE = "RESOURCE_OUT_OF_SCOPE"
    EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    INTERNAL_ERROR = "INTERNAL_ERROR"
