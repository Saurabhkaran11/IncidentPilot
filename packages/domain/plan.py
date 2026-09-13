"""The recovery plan and its digest.

The plan is the one artifact that turns an advisory diagnosis into an
executable, approvable change. Its digest is what an ``Approval`` actually
authorizes -- see docs/decisions/0003-plan-digest-and-approval.md.

Canonicalization rule (do not change without a new ``schema_version`` and a
migration note, because it changes every previously-issued digest):

    canonical_bytes(plan) = UTF-8 JSON of an *explicitly ordered* object
    containing exactly the execution-relevant fields listed in
    ``_CANONICAL_FIELD_ORDER``, with ``json.dumps(..., sort_keys=False,
    separators=(",", ":"))`` so there is exactly one byte sequence per plan.

Anything not in that field list (presentation text, evidence summaries,
created_at) must never affect the digest -- it can change without
invalidating an already-approved plan only if it is *not* execution-relevant,
and nothing outside the listed fields is.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from packages.domain.types import NonEmptyStr, OpaqueId, Sha256Hex

PLAN_SCHEMA_VERSION: Literal["1.0"] = "1.0"
VERIFICATION_PROFILE = "document-result-v1"
#: Bumping this changes plan digests going forward; old approvals stay valid
#: against the digest they were computed with (digest, not policy version,
#: is what an Approval binds to).
PLAN_POLICY_VERSION = "1.0"

DEFAULT_PLAN_TTL_SECONDS = 10 * 60
REPLAY_EXECUTION_DEADLINE_SECONDS = 5 * 60
MAX_REPLAY_REQUESTS = 10


class ReplayRequestRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: OpaqueId
    payload_sha256: Sha256Hex


class RecoveryPlan(BaseModel):
    """Immutable, server-computed execution payload for one incident.

    Every field here is execution-relevant and therefore part of the digest
    (see ``_CANONICAL_FIELD_ORDER`` below) *except* ``plan_id``,
    ``incident_id``, ``created_at``, ``evidence_ids`` and ``policy_version``,
    which are traceability metadata the operator/UI needs but which do not
    change what the executor is allowed to do.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = PLAN_SCHEMA_VERSION
    plan_id: OpaqueId
    incident_id: OpaqueId
    workspace_id: OpaqueId
    app_id: OpaqueId
    account_id: NonEmptyStr
    region: NonEmptyStr
    action: Literal["rollback_demo_alias"] = "rollback_demo_alias"
    function_name: NonEmptyStr
    alias_name: NonEmptyStr
    from_version: NonEmptyStr
    to_version: NonEmptyStr
    expected_alias_revision_id: NonEmptyStr
    routing: Literal["unweighted"] = "unweighted"
    good_version_fingerprint: Sha256Hex
    verification_profile: str = VERIFICATION_PROFILE
    replay_requests: list[ReplayRequestRef] = Field(default_factory=list, max_length=MAX_REPLAY_REQUESTS)
    evidence_ids: list[OpaqueId] = Field(default_factory=list)
    policy_version: str = PLAN_POLICY_VERSION
    expires_at: datetime
    created_at: datetime


#: Explicit, ordered list of digest-relevant fields. Order matters because it
#: fixes the canonical byte sequence -- do not reorder even though JSON key
#: order is not semantically meaningful in general.
_CANONICAL_FIELD_ORDER: tuple[str, ...] = (
    "schema_version",
    "workspace_id",
    "account_id",
    "region",
    "app_id",
    "action",
    "function_name",
    "alias_name",
    "from_version",
    "to_version",
    "expected_alias_revision_id",
    "routing",
    "good_version_fingerprint",
    "verification_profile",
    "replay_requests",
    "expires_at",
)


def canonical_bytes(plan: RecoveryPlan) -> bytes:
    """The exact bytes that get hashed into ``plan.plan_digest``.

    Callers that need to *store* "the exact canonical bytes used" (per the
    build brief's approval section) should persist this value verbatim
    alongside the plan, not recompute it later from a possibly-evolved model.
    """
    ordered: dict[str, object] = {}
    for field in _CANONICAL_FIELD_ORDER:
        value = getattr(plan, field)
        if field == "replay_requests":
            ordered[field] = [
                {"request_id": r.request_id, "payload_sha256": r.payload_sha256} for r in value
            ]
        elif field == "expires_at":
            ordered[field] = _iso(value)
        else:
            ordered[field] = value
    return json.dumps(ordered, sort_keys=False, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _iso(dt: datetime) -> str:
    """Digest timestamps are always UTC; formatting must be byte-stable."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_plan_digest(plan: RecoveryPlan) -> str:
    """``sha256:<hex>`` digest of :func:`canonical_bytes`."""
    return "sha256:" + hashlib.sha256(canonical_bytes(plan)).hexdigest()


def verify_plan_digest(plan: RecoveryPlan, expected_digest: str) -> bool:
    """Constant-time-ish comparison used at approval time.

    A mismatch here means the client is approving a different plan than the
    one it displayed (stale UI, tampered request, or a plan that was
    superseded by a new investigation) -- reject with ``PLAN_DIGEST_MISMATCH``,
    never "helpfully" approve the current plan instead.
    """
    import hmac

    return hmac.compare_digest(compute_plan_digest(plan), expected_digest)
