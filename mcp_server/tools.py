"""The five evidence tools' logic, independent of MCP transport.

Kept transport-agnostic (returns plain dicts matching
``contracts/mcp/evidence-tools.json`` output schemas exactly) so:

* ``mcp_server/server.py`` can expose these over stdio for Strands, and
* ``tests/contract/test_mcp_tools_schema.py`` can validate every response
  against the real JSON Schema without spawning a subprocess, and
* ``agent/stub_model.py`` (the offline deterministic investigator) can call
  these functions directly, guaranteeing the stub sees the exact same
  evidence shape the real Bedrock-backed agent does.

Every tool: (1) rejects an incident_id outside the bound run context, (2)
persists what it read as an ``Evidence`` record before returning a bounded
summary of it, (3) never returns secrets or raw environment variables --
only the allowlisted fields the output schema names.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp_server.context import RunContext
from packages.aws.gateway import AwsGateway
from packages.domain.enums import McpErrorCode, SourceType
from packages.domain.ids import new_id
from packages.domain.models import Evidence
from packages.storage.interface import ControlPlaneStore

SCHEMA_VERSION = "1.0"
MAX_SUMMARY_CHARS = 2000
RUNBOOK_PATH = Path(__file__).resolve().parent.parent / "demo" / "manifests" / "document-alias-rollback-v1.runbook.json"


def _now() -> datetime:
    return datetime.now(UTC)


def _hash_payload(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _store_evidence(
    store: ControlPlaneStore,
    ctx: RunContext,
    *,
    source_type: SourceType,
    tool_name: str,
    source_ref: str,
    observed_at: datetime,
    payload: dict[str, Any],
    summary: str,
) -> Evidence:
    bounded_summary = summary[:MAX_SUMMARY_CHARS]
    evidence = Evidence(
        evidence_id=new_id("ev"),
        incident_id=ctx.incident_id,
        run_id=ctx.run_id,
        source_type=source_type,
        tool_name=tool_name,
        source_ref=source_ref,
        observed_at=observed_at,
        retrieved_at=_now(),
        payload=payload,
        payload_sha256=_hash_payload(payload),
        summary=bounded_summary,
        truncated=len(summary) > MAX_SUMMARY_CHARS,
    )
    store.put_evidence(evidence)
    return evidence


def _evidence_summary(ev: Evidence) -> dict[str, Any]:
    return {
        "evidence_id": ev.evidence_id,
        "source_type": ev.source_type.value,
        "source_ref": ev.source_ref,
        "observed_at": ev.observed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "retrieved_at": ev.retrieved_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "payload_sha256": ev.payload_sha256,
        "summary": ev.summary,
    }


def _ok(
    data: dict[str, Any], evidence: list[Evidence], *, truncated: bool = False, warnings: list[str] | None = None
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "data": data,
        "evidence": [_evidence_summary(e) for e in evidence],
        "truncated": truncated,
        "warnings": warnings or [],
        "error": None,
    }


def _error(code: McpErrorCode, message: str, *, retryable: bool = False) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "error",
        "data": None,
        "evidence": [],
        "truncated": False,
        "warnings": [],
        "error": {"code": code.value, "message": message, "retryable": retryable},
    }


def _out_of_scope(incident_id: str) -> dict[str, Any]:
    return _error(
        McpErrorCode.RESOURCE_OUT_OF_SCOPE,
        f"incident_id {incident_id!r} is outside this investigation's bound run context",
    )


# --------------------------------------------------------------------------
# 1. get_incident_context
# --------------------------------------------------------------------------


def get_incident_context(ctx: RunContext, store: ControlPlaneStore, incident_id: str) -> dict[str, Any]:
    if not ctx.in_scope(incident_id):
        return _out_of_scope(incident_id)

    incident = store.get_incident(incident_id)
    if incident is None:
        return _error(McpErrorCode.EVIDENCE_UNAVAILABLE, "incident record not found")

    app_config = store.get_app_config(incident.app_id)
    if app_config is None:
        return _error(McpErrorCode.EVIDENCE_UNAVAILABLE, "application is not registered")

    observed_at = _now()
    data = {
        "incident_id": incident.incident_id,
        "app_id": incident.app_id,
        "demo_run_id": incident.demo_run_id,
        "state": incident.state.value,
        "mode": incident.mode.value,
        "first_failure_at": (incident.impact.first_failure_at or incident.created_at).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expected_results_table_ref": app_config.expected_results_table_ref,
        "deployment_manifest_ref": f"deployment_manifest:{incident.app_id}",
        "allowed_runbook_id": app_config.runbook_id,
        "failed_request_count": min(incident.impact.failed_requests, 10),
        "policy_context": {
            "source": "deployment_manifest",
            "processor_role_fingerprint": app_config.processor_role_fingerprint,
            "allowed_results_table_ref": app_config.expected_results_table_ref,
            "observed_at": observed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }
    evidence = _store_evidence(
        store,
        ctx,
        source_type=SourceType.DEPLOYMENT_MANIFEST,
        tool_name="get_incident_context",
        source_ref=f"deployment_manifest:{incident.app_id}",
        observed_at=observed_at,
        payload=data,
        summary=(
            f"Incident {incident.incident_id} state={incident.state.value}; "
            f"{data['failed_request_count']} failed request(s); "
            f"expected results table={app_config.expected_results_table_ref}."
        ),
    )
    return _ok(data, [evidence])


# --------------------------------------------------------------------------
# 2. get_error_evidence
# --------------------------------------------------------------------------


def get_error_evidence(
    ctx: RunContext,
    store: ControlPlaneStore,
    gateway: AwsGateway,
    incident_id: str,
    window_minutes: int = 15,
    limit: int = 50,
) -> dict[str, Any]:
    if not ctx.in_scope(incident_id):
        return _out_of_scope(incident_id)

    incident = store.get_incident(incident_id)
    if incident is None:
        return _error(McpErrorCode.EVIDENCE_UNAVAILABLE, "incident record not found")
    app_config = store.get_app_config(incident.app_id)
    if app_config is None:
        return _error(McpErrorCode.EVIDENCE_UNAVAILABLE, "application is not registered")

    window_end = _now()
    from datetime import timedelta

    window_start = window_end - timedelta(minutes=window_minutes)

    try:
        log_events = gateway.get_error_log_events(app_config.log_group_name, window_minutes, limit)
    except Exception as exc:  # pragma: no cover - defensive: real CloudWatch calls can fail
        return _error(McpErrorCode.EVIDENCE_UNAVAILABLE, f"could not read log group: {exc}", retryable=True)

    evidence_records = [
        _store_evidence(
            store,
            ctx,
            source_type=SourceType.CLOUDWATCH_LOG,
            tool_name="get_error_evidence",
            source_ref=le.evidence_source_ref,
            observed_at=le.timestamp,
            payload={
                "request_id": le.request_id,
                "error_type": le.error_type,
                "message": le.message,
            },
            summary=f"{le.error_type}: {le.message}",
        )
        for le in log_events
    ]
    events_out = [
        {
            "evidence_id": ev.evidence_id,
            "timestamp": ev.observed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "request_id": ev.payload["request_id"],
            "error_type": ev.payload["error_type"],
            "message": ev.payload["message"],
        }
        for ev in evidence_records
    ]
    data = {
        "window_start": window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_end": window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "events": events_out,
    }
    # The gateway already applies `limit` server-side; hitting it exactly is
    # our best available truncation signal without a second COUNT query.
    truncated = len(events_out) >= limit
    return _ok(data, evidence_records, truncated=truncated)


# --------------------------------------------------------------------------
# 3. get_release_diff
# --------------------------------------------------------------------------


def get_release_diff(
    ctx: RunContext, store: ControlPlaneStore, gateway: AwsGateway, incident_id: str
) -> dict[str, Any]:
    if not ctx.in_scope(incident_id):
        return _out_of_scope(incident_id)

    incident = store.get_incident(incident_id)
    if incident is None:
        return _error(McpErrorCode.EVIDENCE_UNAVAILABLE, "incident record not found")
    app_config = store.get_app_config(incident.app_id)
    if app_config is None:
        return _error(McpErrorCode.EVIDENCE_UNAVAILABLE, "application is not registered")

    try:
        alias_state = gateway.get_alias_state(app_config.function_name, app_config.alias_name)
    except Exception as exc:
        return _error(McpErrorCode.EVIDENCE_UNAVAILABLE, f"could not read alias configuration: {exc}", retryable=True)

    known_good = store.get_known_good_deployment(app_config.app_id)
    current_entry = store.get_deployment_version(app_config.app_id, alias_state.current_version)

    changes: list[dict[str, Any]] = []
    schema_compatible = True
    processor_role_matches = True
    known_good_fp: str | None = None
    known_good_version: str | None = None
    known_good_verified_at: str | None = None

    if known_good is not None:
        known_good_fp = known_good.config_fingerprint
        known_good_version = known_good.version
        known_good_verified_at = (
            known_good.canary_verified_at.strftime("%Y-%m-%dT%H:%M:%SZ") if known_good.canary_verified_at else None
        )
        if current_entry is not None:
            if current_entry.results_table_ref != known_good.results_table_ref:
                changes.append(
                    {
                        "field": "RESULTS_TABLE",
                        "before": known_good.results_table_ref,
                        "after": current_entry.results_table_ref,
                    }
                )
            if current_entry.schema_version_field != known_good.schema_version_field:
                changes.append(
                    {
                        "field": "SCHEMA_VERSION",
                        "before": known_good.schema_version_field,
                        "after": current_entry.schema_version_field,
                    }
                )
                schema_compatible = False
            processor_role_matches = current_entry.processor_role_fingerprint == known_good.processor_role_fingerprint

    data = {
        "alias_name": alias_state.alias_name,
        "current_version": alias_state.current_version,
        "known_good_version": known_good_version,
        "alias_revision_id": alias_state.alias_revision_id,
        "weighted_routing": alias_state.weighted_routing,
        "current_config_fingerprint": alias_state.config_fingerprint,
        "known_good_config_fingerprint": known_good_fp,
        "known_good_health_verified_at": known_good_verified_at,
        "schema_compatible": schema_compatible,
        "changes": changes,
        "processor_role_matches_manifest": processor_role_matches,
    }
    evidence = _store_evidence(
        store,
        ctx,
        source_type=SourceType.LAMBDA_CONFIGURATION,
        tool_name="get_release_diff",
        source_ref=f"lambda_alias:{app_config.function_name}:{app_config.alias_name}",
        observed_at=alias_state.updated_at,
        payload=data,
        summary=(
            f"alias {alias_state.alias_name!r} -> version {alias_state.current_version} "
            f"(known-good={known_good_version}); {len(changes)} allowlisted field(s) differ."
        ),
    )
    return _ok(data, [evidence])


# --------------------------------------------------------------------------
# 4. get_pending_request_summary
# --------------------------------------------------------------------------


def get_pending_request_summary(ctx: RunContext, store: ControlPlaneStore, incident_id: str) -> dict[str, Any]:
    if not ctx.in_scope(incident_id):
        return _out_of_scope(incident_id)

    incident = store.get_incident(incident_id)
    if incident is None:
        return _error(McpErrorCode.EVIDENCE_UNAVAILABLE, "incident record not found")

    requests = store.list_demo_requests(incident.demo_run_id)[:10]
    counts = {"failed": 0, "completed": 0, "pending": 0, "needs_attention": 0}
    out_requests = []
    for req in requests:
        status = req.status.value
        if status in ("accepted", "processing"):
            counts["pending"] += 1
        elif status in counts:
            counts[status] += 1
        out_requests.append(
            {
                "request_id": req.request_id,
                "payload_sha256": req.payload_sha256,
                "status": status,
                "result_id": req.result_id,
            }
        )

    data = {
        "demo_run_id": incident.demo_run_id,
        "total": len(requests),
        "failed": counts["failed"],
        "completed": counts["completed"],
        "pending": counts["pending"],
        "needs_attention": counts["needs_attention"],
        "requests": out_requests,
    }
    evidence = _store_evidence(
        store,
        ctx,
        source_type=SourceType.REQUEST_INBOX,
        tool_name="get_pending_request_summary",
        source_ref=f"request_inbox:{incident.demo_run_id}",
        observed_at=_now(),
        payload=data,
        summary=(
            f"{data['failed']} failed, {data['completed']} completed, {data['pending']} pending "
            f"of {data['total']} request(s) in run {incident.demo_run_id}."
        ),
    )
    return _ok(data, [evidence])


# --------------------------------------------------------------------------
# 5. get_runbook
# --------------------------------------------------------------------------


def get_runbook(ctx: RunContext, store: ControlPlaneStore, incident_id: str, runbook_id: str) -> dict[str, Any]:
    if not ctx.in_scope(incident_id):
        return _out_of_scope(incident_id)
    if runbook_id != "document-alias-rollback-v1":
        return _error(McpErrorCode.INVALID_ARGUMENT, f"unknown runbook_id {runbook_id!r}")

    try:
        raw = json.loads(RUNBOOK_PATH.read_text())
    except FileNotFoundError:
        return _error(McpErrorCode.EVIDENCE_UNAVAILABLE, "runbook manifest missing")

    evidence = _store_evidence(
        store,
        ctx,
        source_type=SourceType.RUNBOOK,
        tool_name="get_runbook",
        source_ref=f"runbook:{raw['runbook_id']}:{raw['version']}",
        observed_at=_now(),
        payload=raw,
        summary=f"Runbook {raw['runbook_id']} v{raw['version']}: {raw['title']}.",
    )
    return _ok(raw, [evidence])
