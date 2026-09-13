#!/usr/bin/env python
"""Drive the whole product through its HTTP API, exactly as the UI will.

    python scripts/smoke_api.py [--data-dir data_smoke]

Uses FastAPI's TestClient (no server process needed) and runs the worker
inline between steps, so this exercises the real routes, real auth, real
idempotency handling, and the real worker/executor -- not a shortcut path.
If this passes, the UI has a working backend.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os  # noqa: E402


def log(msg: str) -> None:
    print(f"[api-smoke] {msg}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data_smoke")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if data_dir.exists():
        shutil.rmtree(data_dir)
    os.environ["INCIDENTPILOT_DATA_DIR"] = str(data_dir)
    os.environ["INCIDENTPILOT_MODE"] = "fixture"
    os.environ["INCIDENTPILOT_AGENT_MODE"] = "stub"

    from fastapi.testclient import TestClient

    from services.api.main import create_app

    # TestClient defaults its peer address to "testclient"; the API refuses
    # non-loopback callers, so present a real loopback address rather than
    # weakening that check for tests. The context manager runs lifespan
    # startup, which seeds the fixture world.
    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        return _run(client, data_dir)


def _run(client, data_dir: Path) -> int:
    from packages.domain.enums import Mode
    from services.api.config import get_gateway, get_store
    from services.worker.loop import drain_once

    store, gateway = get_store(), get_gateway()

    def drain() -> None:
        drain_once(store, gateway, mode=Mode.FIXTURE, data_dir=str(data_dir), agent_mode="stub")

    def key(name: str) -> dict[str, str]:
        return {"Idempotency-Key": f"smoke-{name}-0001"}

    assert client.get("/healthz").json()["status"] == "ok"
    caps = client.get("/v1/capabilities").json()
    log(f"capabilities: mode={caps['mode']} agent_mode={caps['agent_mode']} features={caps['features']}")

    run_id = client.post("/v1/demo/runs", json={"app_id": "document-demo"}, headers=key("run")).json()["demo_run_id"]
    log(f"created demo run {run_id}")

    # Idempotency: same key + same body replays, different body conflicts.
    assert (
        client.post("/v1/demo/runs", json={"app_id": "document-demo"}, headers=key("run")).json()["demo_run_id"]
        == run_id
    )
    conflict = client.post("/v1/demo/runs", json={"app_id": "other-app"}, headers=key("run"))
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    log("idempotency: replay returns the same run; a different body is 409")

    # Fault injection is an owner-only CLI action, never an HTTP route.
    app_config = store.get_app_config("document-demo")
    gateway.force_set_alias(app_config.function_name, app_config.alias_name, "7")
    log("fault injected out-of-band (alias -> faulty version 7)")

    request_ids = []
    for i, fixture_id in enumerate(["invoice-example-a", "invoice-example-b", "invoice-example-c"]):
        body = {
            "demo_run_id": run_id,
            "document": {"fixture_id": fixture_id, "document_type": "invoice", "page_count": 2},
        }
        resp = client.post("/v1/demo/requests", json=body, headers=key(f"req{i}"))
        assert resp.status_code == 202, resp.text
        request_ids.append(resp.json()["request_id"])
    log(f"submitted {len(request_ids)} requests")

    # Unknown fields must be rejected, not silently ignored.
    bad = client.post(
        "/v1/demo/requests",
        json={
            "demo_run_id": run_id,
            "document": {"fixture_id": "invoice-example-a", "document_type": "invoice", "page_count": 1},
            "role_arn": "arn:aws:iam::1:role/x",
        },
        headers=key("bad"),
    )
    assert bad.status_code == 422, bad.text
    log("unknown request field rejected with 422")

    drain()  # dispatch -> failures -> incident -> investigation -> plan

    incidents = client.get("/v1/incidents").json()["items"]
    assert incidents, "no incident was opened"
    incident_id = incidents[0]["incident_id"]
    incident = client.get(f"/v1/incidents/{incident_id}").json()
    log(f"incident {incident_id}: state={incident['state']} failed={incident['impact']['failed_requests']}")
    log(f"  diagnosis: {incident['diagnosis']['assessment']} -> {incident['diagnosis']['recommended_action']}")
    log(f"  summary: {incident['diagnosis']['summary']}")

    events = client.get(f"/v1/incidents/{incident_id}/events").json()
    log(f"timeline has {len(events['items'])} events: {[e['type'] for e in events['items']]}")

    evidence_id = incident["diagnosis"]["evidence_ids"][0]
    evidence = client.get(f"/v1/incidents/{incident_id}/evidence/{evidence_id}").json()
    log(f"evidence {evidence_id}: source={evidence['source_type']} sha256={evidence['payload_sha256'][:16]}...")

    plan_id = incident["active_plan_id"]
    assert plan_id, "no plan was built"
    plan_resp = client.get(f"/v1/incidents/{incident_id}/plans/{plan_id}").json()
    log(f"plan: {plan_resp['presentation']['confirmation']}")
    log(f"  digest={plan_resp['plan_digest'][:24]}...")

    # A tampered digest must be refused.
    tampered = client.post(
        f"/v1/incidents/{incident_id}/decisions",
        json={
            "plan_id": plan_id,
            "plan_digest": "sha256:" + "0" * 64,
            "expected_incident_version": incident["version"],
            "decision": "approve",
        },
        headers=key("tampered"),
    )
    assert tampered.status_code == 409 and tampered.json()["error"]["code"] == "PLAN_DIGEST_MISMATCH", tampered.text
    log("tampered plan digest rejected")

    # A stale incident version must be refused.
    stale = client.post(
        f"/v1/incidents/{incident_id}/decisions",
        json={
            "plan_id": plan_id,
            "plan_digest": plan_resp["plan_digest"],
            "expected_incident_version": 1,
            "decision": "approve",
        },
        headers=key("stale"),
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "STALE_INCIDENT_VERSION", stale.text
    log("stale incident version rejected")

    approve = client.post(
        f"/v1/incidents/{incident_id}/decisions",
        json={
            "plan_id": plan_id,
            "plan_digest": plan_resp["plan_digest"],
            "expected_incident_version": incident["version"],
            "decision": "approve",
        },
        headers=key("approve"),
    )
    assert approve.status_code == 202, approve.text
    execution_op = approve.json()["operation_id"]
    log(f"approved; execution queued as {execution_op}")

    drain()  # executor: apply -> verify -> replay -> receipt

    op = client.get(f"/v1/operations/{execution_op}").json()
    log(f"execution operation: status={op['status']} phase={op['phase']}")

    final = client.get(f"/v1/incidents/{incident_id}").json()
    receipt = client.get(f"/v1/incidents/{incident_id}/receipt").json()
    log(f"final state: {final['state']}; receipt outcome: {receipt['outcome']}")
    for check in receipt["verification"]:
        log(f"  check {check['check']}: passed={check['passed']}")
    for req in receipt["requests"]:
        log(f"  request {req['request_id']}: {req['outcome']}")
    log(f"  unresolved: {receipt['unresolved_request_ids']}")

    markdown = client.get(f"/v1/incidents/{incident_id}/receipt?format=markdown")
    assert markdown.status_code == 200 and markdown.text.startswith("# Recovery receipt")
    log(f"markdown receipt renders ({len(markdown.text)} chars)")

    for request_id in request_ids:
        state = client.get(f"/v1/demo/requests/{request_id}").json()
        assert state["status"] == "completed", f"{request_id} is {state['status']}"
    log("all three requests verified completed via the API")

    assert final["state"] == "recovered", final["state"]
    assert receipt["outcome"] == "recovered"
    assert receipt["unresolved_request_ids"] == []
    log("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
