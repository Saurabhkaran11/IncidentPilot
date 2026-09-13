#!/usr/bin/env python
"""Publish demo versions G and B and record their real numbers in the manifest.

Run this once, after ``cd infra && npx cdk deploy``:

    python scripts/deploy_demo_workload.py --region us-east-1

What it does, in order (build brief section 5, steps 1-2):

1. Reads the deployed function's current configuration.
2. Publishes version **G** with ``RESULTS_TABLE`` pointing at the expected
   table, points the alias at it, and runs a canary to prove G is healthy
   *before* recording it as known-good. A version nobody has successfully
   invoked is not a rollback target.
3. Publishes version **B** with ``RESULTS_TABLE`` pointing at the quarantine
   table the processor role cannot reach. The execution role is not touched.
4. Writes both, with their **actual** published numeric versions and
   allowlisted-config fingerprints, into ``demo/manifests/``.

It deliberately does NOT point the alias at B. Injecting the fault is a
separate, explicit owner action (``scripts/inject_fault.py``), so deploying
the demo environment never breaks it by itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_DIR = ROOT / "demo" / "manifests"

FUNCTION_NAME = "incidentpilot-demo-processor"
ALIAS_NAME = "live"
EXPECTED_TABLE = "incidentpilot-demo-results"
QUARANTINE_TABLE = "incidentpilot-demo-results-quarantine"
APP_ID = "document-demo"


def config_fingerprint(results_table: str, schema_version: str) -> str:
    """Hash only the allowlisted fields -- never the whole environment.

    Must match ``packages/aws/live_gateway._config_fingerprint`` exactly, or
    the plan policy will see a spurious difference on every comparison.
    """
    allowlisted = {"RESULTS_TABLE": results_table, "SCHEMA_VERSION": schema_version}
    encoded = json.dumps(allowlisted, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _wait_until_updated(client, function_name: str) -> None:
    for _ in range(30):
        state = client.get_function_configuration(FunctionName=function_name)
        if state.get("LastUpdateStatus") != "InProgress":
            return
        time.sleep(2)
    raise TimeoutError(f"{function_name} did not finish updating")


def publish_with_table(client, results_table: str) -> str:
    """Set RESULTS_TABLE, wait, publish, and return the real version number."""
    client.update_function_configuration(
        FunctionName=FUNCTION_NAME,
        Environment={"Variables": {"RESULTS_TABLE": results_table, "SCHEMA_VERSION": "1.0"}},
    )
    _wait_until_updated(client, FUNCTION_NAME)
    published = client.publish_version(FunctionName=FUNCTION_NAME)
    return published["Version"]


def run_canary(client, qualifier: str) -> tuple[bool, str]:
    """Invoke once and report whether it genuinely succeeded."""
    request_id = f"canary_deploy_{int(time.time())}"
    payload = {
        "request_id": request_id,
        "document": {"fixture_id": "invoice-canary", "document_type": "invoice", "page_count": 1},
    }
    response = client.invoke(
        FunctionName=FUNCTION_NAME,
        Qualifier=qualifier,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    # A 200 status with FunctionError set is a failure. Checking only the
    # HTTP status is the classic way to report a broken deploy as healthy.
    function_error = response.get("FunctionError")
    body = response["Payload"].read().decode("utf-8")
    return function_error is None, (function_error or "ok") + ": " + body[:200]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", required=True)
    parser.add_argument("--skip-canary", action="store_true", help="record G without proving it healthy (not recommended)")
    args = parser.parse_args()

    import boto3

    session = boto3.Session(region_name=args.region)
    account_id = session.client("sts").get_caller_identity()["Account"]
    lambda_client = session.client("lambda")

    print(f"account {account_id}, region {args.region}")

    good_version = publish_with_table(lambda_client, EXPECTED_TABLE)
    print(f"published known-good version {good_version} -> {EXPECTED_TABLE}")

    lambda_client.update_alias(FunctionName=FUNCTION_NAME, Name=ALIAS_NAME, FunctionVersion=good_version)
    print(f"alias {ALIAS_NAME!r} -> version {good_version}")

    canary_passed, detail = (True, "skipped")
    canary_verified_at = None
    if not args.skip_canary:
        canary_passed, detail = run_canary(lambda_client, ALIAS_NAME)
        canary_verified_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"canary on version {good_version}: passed={canary_passed} ({detail})")
        if not canary_passed:
            print("\nRefusing to record an unverified version as known-good.", file=sys.stderr)
            print("Fix the deployment before continuing; a rollback target must be proven healthy.", file=sys.stderr)
            return 1

    bad_version = publish_with_table(lambda_client, QUARANTINE_TABLE)
    print(f"published faulty version {bad_version} -> {QUARANTINE_TABLE} (processor role has no access)")

    # Leave the alias on G. Breaking the demo is an explicit separate step.
    lambda_client.update_alias(FunctionName=FUNCTION_NAME, Name=ALIAS_NAME, FunctionVersion=good_version)

    role_arn = f"arn:aws:iam::{account_id}:role/incidentpilot-demo-processor-role"
    role_fingerprint = hashlib.sha256(role_arn.encode()).hexdigest()
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    app_config = {
        "app_id": APP_ID,
        "workspace_id": "workspace_demo",
        "label": "Document request processor",
        "region": args.region,
        "account_id": account_id,
        "function_name": FUNCTION_NAME,
        "alias_name": ALIAS_NAME,
        "log_group_name": f"/aws/lambda/{FUNCTION_NAME}",
        "expected_results_table_ref": EXPECTED_TABLE,
        "forbidden_results_table_ref": QUARANTINE_TABLE,
        "processor_role_fingerprint": role_fingerprint,
        "runbook_id": "document-alias-rollback-v1",
        "schema_version": "1.0",
    }
    deployments = [
        {
            "app_id": APP_ID,
            "version": good_version,
            "results_table_ref": EXPECTED_TABLE,
            "schema_version_field": "1.0",
            "config_fingerprint": config_fingerprint(EXPECTED_TABLE, "1.0"),
            "is_known_good": True,
            "published_at": now,
            "canary_passed": canary_passed,
            "canary_verified_at": canary_verified_at,
            "processor_role_fingerprint": role_fingerprint,
        },
        {
            "app_id": APP_ID,
            "version": bad_version,
            "results_table_ref": QUARANTINE_TABLE,
            "schema_version_field": "1.0",
            "config_fingerprint": config_fingerprint(QUARANTINE_TABLE, "1.0"),
            "is_known_good": False,
            "published_at": now,
            "canary_passed": False,
            "canary_verified_at": None,
            "processor_role_fingerprint": role_fingerprint,
        },
    ]

    (MANIFEST_DIR / "document-demo.app.json").write_text(json.dumps(app_config, indent=2) + "\n")
    (MANIFEST_DIR / "document-demo.deployments.json").write_text(json.dumps(deployments, indent=2) + "\n")
    print(f"\nwrote trusted manifests to {MANIFEST_DIR.relative_to(ROOT)}")
    print(f"  known-good = {good_version}, faulty = {bad_version}")
    print("\nNext:")
    print(f"  INCIDENTPILOT_MODE=aws_live python scripts/run_api.py")
    print(f"  python scripts/inject_fault.py --version {bad_version}   # break it on purpose")
    return 0


if __name__ == "__main__":
    sys.exit(main())
