"""Plan canonicalization: what an approval actually binds to (brief section 10)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from packages.domain.plan import (
    RecoveryPlan,
    ReplayRequestRef,
    compute_plan_digest,
    verify_plan_digest,
)


def sample_plan() -> RecoveryPlan:
    return RecoveryPlan(
        plan_id="plan_sample",
        incident_id="inc_sample",
        workspace_id="workspace_demo",
        app_id="document-demo",
        account_id="000000000000",
        region="us-east-1",
        function_name="incidentpilot-demo-processor",
        alias_name="live",
        from_version="7",
        to_version="6",
        expected_alias_revision_id="rev-aaa",
        good_version_fingerprint="a" * 64,
        replay_requests=[ReplayRequestRef(request_id="docreq_one", payload_sha256="b" * 64)],
        evidence_ids=["ev_one"],
        expires_at=datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC),
        created_at=datetime(2026, 9, 13, 11, 50, 0, tzinfo=UTC),
    )


DIGEST_RELEVANT_CHANGES = {
    "to_version": {"to_version": "5"},
    "from_version": {"from_version": "8"},
    "expected_alias_revision_id": {"expected_alias_revision_id": "rev-bbb"},
    "workspace_id": {"workspace_id": "workspace_other"},
    "account_id": {"account_id": "111111111111"},
    "region": {"region": "us-west-2"},
    "app_id": {"app_id": "other-app"},
    "function_name": {"function_name": "other-function"},
    "alias_name": {"alias_name": "staging"},
    "routing": {"routing": "weighted"},
    "good_version_fingerprint": {"good_version_fingerprint": "c" * 64},
    "verification_profile": {"verification_profile": "document-result-v2"},
    "expires_at": {"expires_at": datetime(2026, 9, 13, 12, 30, 0, tzinfo=UTC)},
    "replay_request_id": {"replay_requests": [ReplayRequestRef(request_id="docreq_two", payload_sha256="b" * 64)]},
    "replay_payload_hash": {"replay_requests": [ReplayRequestRef(request_id="docreq_one", payload_sha256="d" * 64)]},
}

NON_DIGEST_CHANGES = {
    "plan_id": {"plan_id": "plan_other"},
    "incident_id": {"incident_id": "inc_other"},
    "evidence_ids": {"evidence_ids": ["ev_two", "ev_three"]},
    "created_at": {"created_at": datetime(2026, 9, 13, 11, 0, 0, tzinfo=UTC)},
    "policy_version": {"policy_version": "9.9"},
}


def test_identical_plans_produce_identical_digests():
    assert compute_plan_digest(sample_plan()) == compute_plan_digest(sample_plan())


@pytest.mark.parametrize("update", DIGEST_RELEVANT_CHANGES.values(), ids=DIGEST_RELEVANT_CHANGES)
def test_changing_an_execution_relevant_field_changes_the_digest(update):
    plan = sample_plan()
    assert compute_plan_digest(plan.model_copy(update=update)) != compute_plan_digest(plan)


@pytest.mark.parametrize("update", NON_DIGEST_CHANGES.values(), ids=NON_DIGEST_CHANGES)
def test_changing_a_traceability_field_preserves_the_digest(update):
    plan = sample_plan()
    assert compute_plan_digest(plan.model_copy(update=update)) == compute_plan_digest(plan)


def test_verify_plan_digest_accepts_the_computed_digest():
    plan = sample_plan()
    assert verify_plan_digest(plan, compute_plan_digest(plan))


def test_verify_plan_digest_rejects_a_digest_from_a_different_plan():
    plan = sample_plan()
    other_digest = compute_plan_digest(plan.model_copy(update={"to_version": "5"}))
    assert not verify_plan_digest(plan, other_digest)
