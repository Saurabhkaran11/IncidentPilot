"""Failures the agent must refuse to explain (brief section 14, rows 2 and 3).

Both scenarios produce real ``AccessDenied`` errors. Neither is corroborated
by a deployment/configuration difference, so neither may become an action.
"""

from __future__ import annotations

from packages.domain.enums import Assessment, IncidentState, RecommendedAction
from packages.domain.plan_policy import PlanPolicyRejection


def test_access_denied_without_a_configuration_difference_is_insufficient_evidence(world):
    # A newly published version keeps the correct results table but can no
    # longer write to it: errors appear with no RESULTS_TABLE diff to explain them.
    world.submit_requests(3)
    world.register_version("8", can_access=False)
    world.point_alias_at("8")
    world.dispatch()
    world.open_incident()

    diagnosis = world.investigate()

    assert diagnosis.assessment == Assessment.INSUFFICIENT_EVIDENCE
    assert diagnosis.recommended_action == RecommendedAction.NO_ACTION
    assert "corroborating" in " ".join(diagnosis.unknowns), "the missing evidence must be named"
    assert world.incident().state == IncidentState.NEEDS_INFORMATION
    assert isinstance(world.build_plan(), PlanPolicyRejection)


def test_the_policy_refuses_a_rollback_that_no_configuration_evidence_supports(world):
    world.submit_requests(3)
    world.register_version("8", can_access=False)
    world.point_alias_at("8")
    world.dispatch()
    world.open_incident()
    world.investigate()

    # Even if the agent had recommended a rollback, the deterministic gate refuses.
    rejection = world.build_plan(action=RecommendedAction.ROLLBACK_ALIAS)

    assert rejection.reason_code == "NO_CONFIG_EVIDENCE"


def test_a_failure_on_the_known_good_version_never_concludes_a_wrong_table_cause(world):
    # Correct table, correct deployment, unrelated failure.
    world.submit_requests(3)
    world.set_version_accessible(world.good_version, can_access=False)
    world.dispatch()
    world.open_incident()

    diagnosis = world.investigate()

    assert diagnosis.assessment == Assessment.INSUFFICIENT_EVIDENCE
    assert diagnosis.recommended_action == RecommendedAction.NO_ACTION

    wrong_table = [h for h in diagnosis.hypotheses if "wrong results table" in h.description.lower()]
    assert wrong_table, "the wrong-table hypothesis should be stated and then ruled out"
    assert all(not h.supporting_evidence_ids for h in wrong_table)
    assert all(h.contradicting_evidence_ids for h in wrong_table)

    assert world.build_plan(action=RecommendedAction.ROLLBACK_ALIAS).reason_code == "ALREADY_ON_KNOWN_GOOD"
