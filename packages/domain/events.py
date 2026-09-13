"""Activity-timeline event type catalog.

Every persisted event uses one of these ``type`` strings so the frontend
timeline and contract tests can enumerate them. Payloads are sanitized dicts
-- never raw model reasoning, never secrets (see build brief section 8).
"""

from __future__ import annotations

from enum import StrEnum


class EventType(StrEnum):
    INCIDENT_DETECTED = "incident.detected"
    INVESTIGATION_STARTED = "investigation.started"
    INVESTIGATION_TOOL_STARTED = "investigation.tool_started"
    INVESTIGATION_TOOL_COMPLETED = "investigation.tool_completed"
    INVESTIGATION_COMPLETED = "investigation.completed"
    PLAN_CREATED = "plan.created"
    PLAN_INVALIDATED = "plan.invalidated"
    DECISION_RECORDED = "decision.recorded"
    EXECUTION_STARTED = "execution.started"
    EXECUTION_RECONCILED = "execution.reconciled"
    VERIFICATION_COMPLETED = "verification.completed"
    REPLAY_STARTED = "replay.started"
    REQUEST_RECONCILED = "request.reconciled"
    RECEIPT_CREATED = "receipt.created"
    INCIDENT_NEEDS_ATTENTION = "incident.needs_attention"
