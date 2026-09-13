"""Every gate in the deterministic plan policy (brief section 8).

A rejection must always name the prerequisite that failed, so each gate gets
its own case built by perturbing exactly one field of an otherwise-eligible
world.
"""

from __future__ import annotations

import pytest

import scripts.seed_fixture as seed_fixture
from packages.domain.config import DeploymentManifestEntry
from packages.domain.enums import RecommendedAction
from packages.domain.plan import MAX_REPLAY_REQUESTS, RecoveryPlan
from packages.domain.plan_policy import PlanPolicyRejection, ReleaseDiffSnapshot, evaluate

APP_CONFIG = seed_fixture.load_app_config()
KNOWN_GOOD = next(e for e in seed_fixture.load_deployment_entries() if e.is_known_good)
FAULTY = next(e for e in seed_fixture.load_deployment_entries() if not e.is_known_good)


def eligible_diff(**overrides) -> ReleaseDiffSnapshot:
    base = {
        "alias_name": APP_CONFIG.alias_name,
        "current_version": FAULTY.version,
        "known_good_version": KNOWN_GOOD.version,
        "alias_revision_id": "rev-aaa",
        "weighted_routing": False,
        "schema_compatible": True,
        "changes": [
            {
                "field": "RESULTS_TABLE",
                "before": KNOWN_GOOD.results_table_ref,
                "after": FAULTY.results_table_ref,
            }
        ],
        "processor_role_matches_manifest": True,
    }
    base.update(overrides)
    return ReleaseDiffSnapshot(**base)


def run_policy(
    *,
    action: RecommendedAction = RecommendedAction.ROLLBACK_ALIAS,
    diff: ReleaseDiffSnapshot | None = None,
    known_good: DeploymentManifestEntry | None = KNOWN_GOOD,
    replay_count: int = 3,
) -> RecoveryPlan | PlanPolicyRejection:
    return evaluate(
        incident_id="inc_sample",
        app_config=APP_CONFIG,
        diagnosis_recommended_action=action,
        diagnosis_evidence_ids=["ev_one"],
        diff=diff or eligible_diff(),
        known_good_entry=known_good,
        replay_request_ids=[(f"docreq_{i}", f"{i:064d}".replace("0", "a")) for i in range(replay_count)],
    )


def test_complete_evidence_produces_an_eligible_bounded_rollback():
    plan = run_policy()

    assert isinstance(plan, RecoveryPlan)
    assert (plan.from_version, plan.to_version) == (FAULTY.version, KNOWN_GOOD.version)
    assert plan.expected_alias_revision_id == "rev-aaa"
    assert plan.good_version_fingerprint == KNOWN_GOOD.config_fingerprint
    assert plan.routing == "unweighted"
    assert plan.expires_at > plan.created_at
    assert len(plan.replay_requests) == 3


def test_a_no_action_diagnosis_is_never_upgraded_into_a_plan():
    rejection = run_policy(action=RecommendedAction.NO_ACTION)
    assert rejection.reason_code == "NO_ACTION_RECOMMENDED"


@pytest.mark.parametrize(
    ("known_good", "diff"),
    [(None, None), (KNOWN_GOOD, eligible_diff(known_good_version=None))],
    ids=["no_manifest_entry", "no_version_in_diff"],
)
def test_without_a_recorded_known_good_version_the_policy_refuses(known_good, diff):
    rejection = run_policy(known_good=known_good, diff=diff)
    assert rejection.reason_code == "NO_KNOWN_GOOD_VERSION"


def test_a_known_good_version_whose_canary_never_passed_is_not_a_rollback_target():
    rejection = run_policy(known_good=KNOWN_GOOD.model_copy(update={"canary_passed": False}))
    assert rejection.reason_code == "KNOWN_GOOD_UNVERIFIED"


def test_an_alias_already_on_the_known_good_version_has_nothing_to_roll_back():
    rejection = run_policy(diff=eligible_diff(current_version=KNOWN_GOOD.version))
    assert rejection.reason_code == "ALREADY_ON_KNOWN_GOOD"


def test_weighted_routing_is_refused_rather_than_quietly_changed():
    rejection = run_policy(diff=eligible_diff(weighted_routing=True))
    assert rejection.reason_code == "WEIGHTED_ROUTING_ACTIVE"


def test_an_incompatible_schema_blocks_the_rollback():
    rejection = run_policy(diff=eligible_diff(schema_compatible=False))
    assert rejection.reason_code == "SCHEMA_INCOMPATIBLE"


def test_a_changed_processor_role_is_out_of_scope_for_an_alias_rollback():
    rejection = run_policy(diff=eligible_diff(processor_role_matches_manifest=False))
    assert rejection.reason_code == "ROLE_MISMATCH"


def test_without_a_configuration_difference_the_policy_refuses():
    rejection = run_policy(diff=eligible_diff(changes=[]))
    assert rejection.reason_code == "NO_CONFIG_EVIDENCE"


def test_a_replay_batch_larger_than_the_cap_is_refused():
    rejection = run_policy(replay_count=MAX_REPLAY_REQUESTS + 1)
    assert rejection.reason_code == "TOO_MANY_REQUESTS"
