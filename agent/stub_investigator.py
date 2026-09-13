"""``agent_mode: "stub"`` -- a deterministic, free, always-available investigator.

Calls the exact same evidence functions the real MCP server exposes
(``mcp_server.tools``, in-process rather than over stdio) and applies a
fixed decision table instead of an LLM. This exists for three reasons:

1. CI and ``tests/policy/`` need a fast, zero-cost, fully deterministic path
   that never depends on Bedrock model access.
2. ``/v1/capabilities`` must be able to say ``agent_mode: "stub"`` honestly
   when no model is configured, instead of silently degrading.
3. It is the executable spec for what "supported" should mean -- the real
   agent (``agent/investigator.py``) is graded against these same eval
   scenarios (``evals/``), not the other way around.

This function performs **no** creative synthesis; it is intentionally
legible line-by-line so an interviewer can see exactly which evidence fields
drive which conclusion (build brief section 14's eval matrix).
"""

from __future__ import annotations

import time

from agent.schema import DiagnosisOut, HypothesisOut
from mcp_server import tools as evidence
from mcp_server.context import RunContext
from packages.aws.gateway import AwsGateway
from packages.domain.enums import Assessment, RecommendedAction
from packages.storage.interface import ControlPlaneStore

MAX_TOOL_CALLS = 12
MAX_DURATION_SECONDS = 90


class StubInvestigationError(RuntimeError):
    pass


def investigate_stub(
    ctx: RunContext, store: ControlPlaneStore, gateway: AwsGateway, incident_id: str
) -> tuple[DiagnosisOut, int, int]:
    """Returns ``(diagnosis, tool_call_count, duration_ms)``."""
    started = time.monotonic()
    tool_calls = 0

    def _budgeted(fn, *args, **kwargs):
        nonlocal tool_calls
        tool_calls += 1
        if tool_calls > MAX_TOOL_CALLS:
            raise StubInvestigationError(f"exceeded {MAX_TOOL_CALLS} tool calls")
        if time.monotonic() - started > MAX_DURATION_SECONDS:
            raise StubInvestigationError(f"exceeded {MAX_DURATION_SECONDS}s investigation budget")
        return fn(*args, **kwargs)

    ctx_resp = _budgeted(evidence.get_incident_context, ctx, store, incident_id)
    if ctx_resp["status"] == "error":
        return (
            DiagnosisOut(
                incident_id=incident_id,
                assessment=Assessment.INSUFFICIENT_EVIDENCE,
                summary=f"Could not read incident context: {ctx_resp['error']['message']}",
                unknowns=["incident context unavailable"],
                recommended_action=RecommendedAction.NO_ACTION,
            ),
            tool_calls,
            _elapsed_ms(started),
        )

    diff_resp = _budgeted(evidence.get_release_diff, ctx, store, gateway, incident_id)
    error_resp = _budgeted(evidence.get_error_evidence, ctx, store, gateway, incident_id)
    _budgeted(evidence.get_pending_request_summary, ctx, store, incident_id)
    _budgeted(evidence.get_runbook, ctx, store, incident_id, "document-alias-rollback-v1")

    evidence_ids: list[str] = []
    for resp in (ctx_resp, diff_resp, error_resp):
        evidence_ids.extend(e["evidence_id"] for e in resp.get("evidence", []))

    diagnosis = _decide(incident_id, ctx_resp, diff_resp, error_resp, evidence_ids)
    return diagnosis, tool_calls, _elapsed_ms(started)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _decide(
    incident_id: str,
    ctx_resp: dict,
    diff_resp: dict,
    error_resp: dict,
    evidence_ids: list[str],
) -> DiagnosisOut:
    diff_data = diff_resp.get("data") if diff_resp.get("status") != "error" else None
    error_events = (error_resp.get("data") or {}).get("events", []) if error_resp.get("status") != "error" else []

    access_denied = any(e["error_type"] == "AccessDeniedException" or "AccessDenied" in e["message"] for e in error_events)

    # Scenario: no known-good version recorded at all -> cannot conclude anything.
    if diff_data is None or diff_data.get("known_good_version") is None:
        return DiagnosisOut(
            incident_id=incident_id,
            assessment=Assessment.INSUFFICIENT_EVIDENCE,
            summary="No known-good deployment is recorded for this application; cannot evaluate a rollback.",
            unknowns=["no recorded known-good version"],
            evidence_ids=evidence_ids,
            recommended_action=RecommendedAction.NO_ACTION,
        )

    already_on_good = diff_data["current_version"] == diff_data["known_good_version"]
    table_changed = any(c["field"] == "RESULTS_TABLE" for c in diff_data.get("changes", []))

    # Scenario: currently on the known-good version -- any failure here is
    # unrelated to a bad deployment, so never conclude a wrong-table cause.
    if already_on_good:
        return DiagnosisOut(
            incident_id=incident_id,
            assessment=Assessment.INSUFFICIENT_EVIDENCE,
            summary="The alias already points at the known-good version; the failure is not a deployment mismatch.",
            hypotheses=[
                HypothesisOut(
                    description="Deployment points at the wrong results table.",
                    contradicting_evidence_ids=evidence_ids,
                )
            ],
            unknowns=["failure cause with the correct deployment active"],
            evidence_ids=evidence_ids,
            recommended_action=RecommendedAction.NO_ACTION,
        )

    # Scenario: AccessDenied evidence with a corroborating RESULTS_TABLE
    # diff and an unchanged processor role -> supported wrong-table cause.
    if access_denied and table_changed and diff_data.get("processor_role_matches_manifest"):
        return DiagnosisOut(
            incident_id=incident_id,
            assessment=Assessment.SUPPORTED,
            summary=(
                f"Version {diff_data['current_version']} points RESULTS_TABLE at a table the processor "
                f"role cannot access; the known-good version {diff_data['known_good_version']} used a "
                "different, accessible table. The execution role is unchanged."
            ),
            hypotheses=[
                HypothesisOut(
                    description="Deployment points at the wrong results table (config-only regression).",
                    supporting_evidence_ids=evidence_ids,
                ),
                HypothesisOut(
                    description="The processor's IAM permissions were narrowed.",
                    contradicting_evidence_ids=evidence_ids,
                ),
            ],
            evidence_ids=evidence_ids,
            recommended_action=RecommendedAction.ROLLBACK_ALIAS,
        )

    # Scenario: AccessDenied with no corroborating deployment/config evidence
    # -> AccessDenied alone is insufficient (build brief section 5, step 4).
    if access_denied and not table_changed:
        return DiagnosisOut(
            incident_id=incident_id,
            assessment=Assessment.INSUFFICIENT_EVIDENCE,
            summary="Requests are failing with AccessDenied, but no deployment or configuration difference corroborates a wrong-target cause.",
            hypotheses=[
                HypothesisOut(
                    description="Deployment points at the wrong results table.",
                    contradicting_evidence_ids=evidence_ids,
                )
            ],
            unknowns=["a corroborating configuration or deployment difference"],
            evidence_ids=evidence_ids,
            recommended_action=RecommendedAction.NO_ACTION,
        )

    # Default: evidence doesn't fit a known pattern -- abstain rather than guess.
    return DiagnosisOut(
        incident_id=incident_id,
        assessment=Assessment.INSUFFICIENT_EVIDENCE,
        summary="Evidence collected does not clearly support or rule out a deployment-related cause.",
        unknowns=["failure cause"],
        evidence_ids=evidence_ids,
        recommended_action=RecommendedAction.NO_ACTION,
    )
