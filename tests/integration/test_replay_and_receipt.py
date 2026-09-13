"""Bounded replay, duplicate protection, and receipt truthfulness (brief section 11)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from packages.domain.enums import (
    IncidentState,
    OperationStatus,
    ReceiptOutcome,
    ReceiptRequestOutcome,
)
from packages.domain.plan import REPLAY_EXECUTION_DEADLINE_SECONDS
from services.executor.replay import run_replay


def test_replaying_an_already_completed_request_returns_the_existing_result(investigated_world):
    world = investigated_world
    plan = world.plan()
    world.approve_and_execute(plan)

    # A duplicate delivery of the same approved batch.
    outcomes = run_replay(
        world.store,
        world.gateway,
        world.app_config,
        plan,
        "op_duplicate",
        deadline=datetime.now(UTC) + timedelta(seconds=REPLAY_EXECUTION_DEADLINE_SECONDS),
    )

    assert {o.outcome for o in outcomes} == {ReceiptRequestOutcome.ALREADY_COMPLETED}
    assert all(world.result_row_count(request_id) == 1 for request_id in world.request_ids)


def test_the_same_request_id_with_a_different_payload_conflicts_without_overwriting(investigated_world):
    world = investigated_world
    world.approve_and_execute(world.plan())
    table_ref = world.app_config.expected_results_table_ref
    request_id = world.request_ids[0]
    original = world.gateway.get_result(table_ref, request_id)

    result = world.gateway.invoke(
        world.app_config.function_name,
        world.app_config.alias_name,
        {
            "request_id": request_id,
            "document": {"fixture_id": "tampered", "document_type": "invoice", "page_count": 9},
        },
    )

    assert result.payload["conflict"] is True
    assert result.payload["already_completed"] is False
    stored = world.gateway.get_result(table_ref, request_id)
    assert stored.payload_sha256 == original.payload_sha256
    assert stored.page_count == original.page_count
    assert world.result_row_count(request_id) == 1


def test_a_partial_replay_reports_unreplayed_requests_instead_of_claiming_them(investigated_world):
    world = investigated_world
    approved_ids = world.request_ids[:2]
    left_out_id = world.request_ids[2]
    plan = world.build_plan(replay_request_ids=approved_ids)

    operation = world.approve_and_execute(plan)

    assert operation.status == OperationStatus.SUCCEEDED
    assert world.incident().state == IncidentState.SERVICE_RESTORED_PENDING_REQUESTS

    receipt = world.receipt()
    assert receipt.outcome == ReceiptOutcome.PARTIAL
    assert all(check.passed for check in receipt.verification), "the service itself did recover"
    assert receipt.unresolved_request_ids == [left_out_id]

    outcomes = {line.request_id: line.outcome for line in receipt.requests}
    assert outcomes[left_out_id] == ReceiptRequestOutcome.NOT_REPLAYED
    assert all(outcomes[r] == ReceiptRequestOutcome.COMPLETED for r in approved_ids)
    assert world.result_row_count(left_out_id) == 0, "an unreplayed request has no result to claim"
