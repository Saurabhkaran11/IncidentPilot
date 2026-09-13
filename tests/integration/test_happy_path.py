"""Brief section 5 end to end: bad table configuration with complete evidence."""

from __future__ import annotations

from packages.domain.enums import (
    Assessment,
    IncidentState,
    OperationStatus,
    ReceiptOutcome,
    ReceiptRequestOutcome,
    RecommendedAction,
)
from packages.domain.plan import compute_plan_digest


def test_a_supported_diagnosis_produces_an_eligible_rollback_plan(investigated_world):
    diagnosis = investigated_world.store.get_latest_diagnosis(investigated_world.incident_id)

    assert diagnosis.assessment == Assessment.SUPPORTED
    assert diagnosis.recommended_action == RecommendedAction.ROLLBACK_ALIAS
    assert diagnosis.evidence_ids
    assert investigated_world.incident().state == IncidentState.AWAITING_APPROVAL

    plan = investigated_world.plan()
    assert (plan.from_version, plan.to_version) == (
        investigated_world.bad_version,
        investigated_world.good_version,
    )
    assert len(plan.replay_requests) == 3
    assert investigated_world.store.get_plan_digest(plan.plan_id) == compute_plan_digest(plan)


def test_an_approved_rollback_recovers_the_service_and_every_affected_request(investigated_world):
    world = investigated_world

    operation = world.approve_and_execute(world.plan())

    assert operation.status == OperationStatus.SUCCEEDED
    assert world.incident().state == IncidentState.RECOVERED
    assert world.alias_version() == world.good_version

    receipt = world.receipt()
    assert receipt.outcome == ReceiptOutcome.RECOVERED
    assert receipt.applied_version == world.good_version
    assert [check.check for check in receipt.verification] == [
        "alias_version",
        "canary_invocation",
        "canary_result_and_hash",
    ]
    assert all(check.passed for check in receipt.verification)
    assert len(receipt.requests) == 3
    assert {line.outcome for line in receipt.requests} == {ReceiptRequestOutcome.COMPLETED}
    assert receipt.unresolved_request_ids == []


def test_the_synthetic_canary_is_excluded_from_customer_request_counts(investigated_world):
    world = investigated_world
    world.approve_and_execute(world.plan())

    receipt = world.receipt()

    assert {line.request_id for line in receipt.requests} == set(world.request_ids)
