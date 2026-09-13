"""Everything the executor must refuse to do (brief section 10).

Each case must end with no recovery claim: no receipt saying ``recovered``,
and no alias left in a state the approval did not authorize.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from packages.domain.enums import IncidentState, OperationStatus, RequestStatus


def test_an_alias_moved_after_approval_stops_with_a_stale_plan_and_no_overwrite(investigated_world):
    world = investigated_world
    plan = world.plan()
    digest = world.store_plan(plan)
    approval = world.approve(plan, digest)
    operation_id = world.queue_execution(plan, approval)

    # Someone else deploys while the approval is waiting.
    world.register_version("8", can_access=True)
    world.point_alias_at("8")

    operation = world.execute(operation_id)

    assert operation.status == OperationStatus.NEEDS_ATTENTION
    assert "STALE_PLAN" in world.needs_attention_codes()
    assert world.alias_version() == "8", "the third party's deployment must not be overwritten"
    assert world.incident().state == IncidentState.NEEDS_ATTENTION
    assert world.receipt() is None


def test_an_expired_plan_cannot_start_a_mutation(investigated_world):
    world = investigated_world
    expired_plan = world.build_plan(now=datetime.now(UTC) - timedelta(hours=1))
    assert expired_plan.expires_at < datetime.now(UTC)

    operation = world.approve_and_execute(expired_plan)

    assert operation.status == OperationStatus.NEEDS_ATTENTION
    assert "PLAN_EXPIRED" in world.needs_attention_codes()
    assert world.alias_version() == world.bad_version, "no mutation was attempted"
    assert world.receipt() is None


def test_an_approval_whose_digest_does_not_match_the_stored_plan_is_refused(investigated_world, caplog):
    world = investigated_world

    with caplog.at_level(logging.ERROR, logger="incidentpilot.transitions"):
        operation = world.approve_and_execute(world.plan(), digest="sha256:" + "0" * 64)

    assert operation.status == OperationStatus.NEEDS_ATTENTION
    assert "PLAN_DIGEST_MISMATCH" in world.needs_attention_codes()
    assert "illegal transition" not in caplog.text, "refusing execution is a modeled edge, not a fallback"
    assert world.alias_version() == world.bad_version
    assert world.receipt() is None


def test_a_failing_canary_after_the_rollback_never_claims_recovery(investigated_world):
    world = investigated_world
    plan = world.plan()

    # The rollback target itself loses access between planning and verification.
    world.set_version_accessible(world.good_version, can_access=False)
    operation = world.approve_and_execute(plan)

    assert operation.status == OperationStatus.NEEDS_ATTENTION
    assert world.incident().state == IncidentState.NEEDS_ATTENTION
    assert world.alias_version() == world.good_version, "the approved rollback did apply"
    assert world.receipt() is None, "a failed health check must never produce a recovery receipt"
    assert all(world.store.get_demo_request(r).status == RequestStatus.FAILED for r in world.request_ids)
