"""The deterministic policy that turns a diagnosis into an executable plan.

Build brief section 8: "Application code constructs the executable plan
from the registered deployment manifest and validates it against fresh
evidence. A deterministic policy requires a recorded healthy version,
compatible schema, current alias pointing to the observed faulty version, no
weighted routing, and adequate evidence of the configuration mismatch. If
any prerequisite is absent, request more information or produce no action."

This module never talks to the model. It takes the agent's ``DiagnosisOut``
(advisory only) plus *fresh* evidence re-read at plan-construction time, and
either returns a fully-formed, digest-ready ``RecoveryPlan`` or a
``PlanPolicyRejection`` naming exactly which prerequisite failed -- so a
rejected plan is always explainable, never a silent no-op.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from packages.domain.config import AppConfig, DeploymentManifestEntry
from packages.domain.ids import new_id
from packages.domain.plan import (
    DEFAULT_PLAN_TTL_SECONDS,
    MAX_REPLAY_REQUESTS,
    RecoveryPlan,
    ReplayRequestRef,
)
from packages.domain.enums import RecommendedAction


@dataclass(frozen=True)
class PlanPolicyRejection:
    reason_code: str
    message: str


@dataclass(frozen=True)
class ReleaseDiffSnapshot:
    """The subset of ``get_release_diff`` output the policy actually needs."""

    alias_name: str
    current_version: str
    known_good_version: str | None
    alias_revision_id: str
    weighted_routing: bool
    schema_compatible: bool
    changes: list[dict[str, str | None]]
    processor_role_matches_manifest: bool


def evaluate(
    *,
    incident_id: str,
    app_config: AppConfig,
    diagnosis_recommended_action: RecommendedAction,
    diagnosis_evidence_ids: list[str],
    diff: ReleaseDiffSnapshot,
    known_good_entry: DeploymentManifestEntry | None,
    replay_request_ids: list[tuple[str, str]],  # (request_id, payload_sha256)
    now: datetime | None = None,
) -> RecoveryPlan | PlanPolicyRejection:
    now = now or datetime.now(UTC)

    if diagnosis_recommended_action != RecommendedAction.ROLLBACK_ALIAS:
        return PlanPolicyRejection("NO_ACTION_RECOMMENDED", "The diagnosis did not recommend a rollback.")

    if known_good_entry is None or diff.known_good_version is None:
        return PlanPolicyRejection("NO_KNOWN_GOOD_VERSION", "No recorded known-good version with a verified canary.")

    if not known_good_entry.canary_passed:
        return PlanPolicyRejection("KNOWN_GOOD_UNVERIFIED", "The known-good version's canary never passed.")

    if diff.current_version == diff.known_good_version:
        return PlanPolicyRejection("ALREADY_ON_KNOWN_GOOD", "The alias already points at the known-good version.")

    if diff.weighted_routing:
        return PlanPolicyRejection("WEIGHTED_ROUTING_ACTIVE", "The alias uses weighted routing; this policy only supports a full rollback.")

    if not diff.schema_compatible:
        return PlanPolicyRejection("SCHEMA_INCOMPATIBLE", "The current and known-good versions use incompatible schemas.")

    if not diff.processor_role_matches_manifest:
        return PlanPolicyRejection(
            "ROLE_MISMATCH", "The processor execution role differs from the trusted manifest; this is an IAM change, out of scope."
        )

    if not diff.changes:
        return PlanPolicyRejection(
            "NO_CONFIG_EVIDENCE", "No allowlisted configuration field differs between the current and known-good versions."
        )

    if len(replay_request_ids) > MAX_REPLAY_REQUESTS:
        return PlanPolicyRejection("TOO_MANY_REQUESTS", f"At most {MAX_REPLAY_REQUESTS} requests may be selected for replay.")

    plan = RecoveryPlan(
        plan_id=new_id("plan"),
        incident_id=incident_id,
        workspace_id=app_config.workspace_id,
        app_id=app_config.app_id,
        account_id=app_config.account_id,
        region=app_config.region,
        function_name=app_config.function_name,
        alias_name=diff.alias_name,
        from_version=diff.current_version,
        to_version=diff.known_good_version,
        expected_alias_revision_id=diff.alias_revision_id,
        good_version_fingerprint=known_good_entry.config_fingerprint,
        replay_requests=[ReplayRequestRef(request_id=r, payload_sha256=h) for r, h in replay_request_ids],
        evidence_ids=diagnosis_evidence_ids,
        expires_at=now + timedelta(seconds=DEFAULT_PLAN_TTL_SECONDS),
        created_at=now,
    )
    return plan
