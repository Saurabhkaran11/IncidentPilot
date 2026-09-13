# Demo manifests

`document-demo.app.json` -- the one registered application (build brief
section 5). `region`/`account_id` are fixture placeholders (moto/sandbox
convention: `us-east-1` / `000000000000`) until a real deployment runs.
`scripts/deploy_demo_workload.py` overwrites them with real values after
`aws lambda publish-version`; see `docs/BUILD_STATUS.md` for current status.

`document-demo.deployments.json` -- the two published versions (G = known
good, B = intentionally faulty) exactly as `scripts/seed_fixture.py` (fixture
mode) or `scripts/deploy_demo_workload.py` (live mode) record them: which
`RESULTS_TABLE` each points to, and a `config_fingerprint` that is
`sha256({"RESULTS_TABLE": ..., "SCHEMA_VERSION": ...})` -- the same
allowlisted-fields hash `get_release_diff` returns, never a hash of the full
Lambda environment.

Fixture mode assigns versions `"6"` (good) and `"7"` (bad) by convention --
matching the illustrative example in `docs/specs/IncidentPilot-API-Contract.md`
so screenshots/tests read consistently. These are **not** real Lambda
version numbers; the build brief explicitly warns "do not assume versions 1
and 2" for the live demonstration. `scripts/deploy_demo_workload.py`
replaces this file with the actual numeric versions AWS returns from
`publish-version` after a real deployment.

`document-alias-rollback-v1.runbook.json` -- the fixed runbook `get_runbook`
serves. Editing its `eligibility_checks`/`contraindications` changes what the
deterministic plan-construction policy in `services/api/plan_policy.py`
requires before it will build a rollback plan.
