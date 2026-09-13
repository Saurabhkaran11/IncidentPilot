# IncidentPilot HTTP API contract

Design version 1.0, September 13, 2026. Proposed application contracts; these are not AWS service endpoints. Implement with FastAPI/Pydantic, export OpenAPI 3.1, and generate frontend types from the validated schema.

## Shared conventions

- Prefix `/v1`. JSON uses snake_case; timestamps are UTC RFC 3339. IDs are opaque strings, not bearer credentials.
- All routes except `/healthz` require the appropriate operator or service identity in live mode. Derive workspace and actor from verified identity and server configuration. Validate issuer, audience/client, signature, expiry, and token use according to the chosen provider; do not merely decode JWTs.
- Local fixture mode can use a fixed development actor only when bound to loopback. Disable it in hosted environments. Public walkthrough data is served through a separate read-only path and contains no live-control credentials.
- Local live mode uses the owner's short-lived random bearer token, issued by a trusted launch command after an AWS STS identity check and mapped server-side to that identity/workspace. Store only the token hash; bind it to the boot session, expire after 30 minutes, and enforce loopback plus the exact UI origin. Browser storage is memory-only. Hosted mode rejects these tokens and uses the configured identity provider. No public token-minting endpoint is part of this contract.
- Authenticated human operators may inspect incidents, start an investigation, approve/reject a plan, and submit bounded demo requests. Fault injection is an owner-only CLI command, not an anonymous HTTP endpoint. Internal event ingestion uses workload identity.
- Mutating POST routes require an `Idempotency-Key` between 8 and 128 characters. Scope it to workspace + principal + method + route. Store a canonical request hash and response reference for at least the lifetime of the demo run. The same key and payload returns the same resource/operation; a different payload returns 409. Event source IDs also deduplicate delivery across callers/transport retries.
- Validate JSON with unknown fields rejected. Enforce body size limits. No endpoint accepts arbitrary AWS ARNs, shell commands, role names, URLs, SQL, or log queries from the browser.
- Every asynchronous acceptance returns HTTP 202 with `operation_id`, `status_url`, `resource_id`, and `status: "accepted"`; create durable state before responding. Optional descriptive IDs such as `request_id`, `incident_id`, and `approval_id` may accompany those required fields. Read routes use HTTP 200. No background task is complete merely because it was accepted.
- Lists use opaque `cursor`, bounded `limit` (default 20, maximum 100), and return `items` and `next_cursor`. A cursor carries no authorization; reauthorize each page.
- Timelines use monotonically increasing per-incident sequence numbers. Poll every two seconds while active, then back off. Stop polling on terminal state or hidden tab. SSE is optional later work.

Common error envelope:

```json
{
  "error": {
    "code": "STALE_PLAN",
    "message": "The deployed alias changed after this plan was prepared.",
    "retryable": false,
    "request_id": "req_api_01",
    "details": {"plan_id": "plan_01"}
  }
}
```

Use 400 for malformed JSON, 401 for missing/invalid authentication, 403 for denied action, 404 for unavailable resources, 409 for state/idempotency conflicts, 422 for schema errors, 429 for rate limits with `Retry-After`, and 503 for unavailable dependencies. Normalize FastAPI validation errors to this envelope. Do not return credentials, stack traces, or sensitive AWS error payloads.

## Endpoint summary

| Method and path | Identity | Purpose |
|---|---|---|
| `GET /healthz` | Public | Process readiness, no configuration details |
| `GET /v1/capabilities` | Operator | Mode and implemented features, registered app IDs |
| `POST /v1/demo/runs` | Operator | Create isolated fixture-request namespace; no fault injection |
| `POST /v1/demo/requests` | Operator | Persist a request and queue a processing attempt |
| `GET /v1/demo/requests/{request_id}` | Operator | Input hash and verified processing status |
| `POST /v1/incidents` | Trusted service | Normalize and deduplicate a real or labeled fixture event |
| `GET /v1/incidents` | Operator | Paginated incident work list |
| `GET /v1/incidents/{incident_id}` | Operator | Incident summary and latest work state |
| `POST /v1/incidents/{incident_id}/investigations` | Operator/service | Queue a bounded investigation or reevaluation |
| `GET /v1/incidents/{incident_id}/events` | Operator | Durable activity events after a sequence number |
| `GET /v1/incidents/{incident_id}/evidence/{evidence_id}` | Operator | Sanitized stored source snapshot |
| `GET /v1/incidents/{incident_id}/plans/{plan_id}` | Operator | Exact immutable recovery plan |
| `POST /v1/incidents/{incident_id}/decisions` | Operator | Approve/reject the plan and queue approved execution |
| `GET /v1/operations/{operation_id}` | Operator/service | Operation progress and result |
| `GET /v1/incidents/{incident_id}/receipt` | Operator | JSON or Markdown recovery receipt |

There is no model-callable approval endpoint and no public `execute arbitrary action` route. Approval and execution dispatch are one application transaction with an outbox. The executor consumes the operation ID and loads all immutable parameters from storage.

## 1. Capabilities and demo runs

`GET /v1/capabilities` returns:

```json
{
  "schema_version": "1.0",
  "mode": "fixture",
  "agent_mode": "bedrock",
  "features": {
    "mcp_evidence": true,
    "alias_rollback": true,
    "bounded_replay": false,
    "agentcore_runtime": false
  },
  "applications": [{"app_id": "document-demo", "label": "Document request processor"}]
}
```

Mode is `fixture` or `aws_live`; agent_mode is `bedrock` or `stub`. A fixture with a stub agent must say so. Values reflect the actual configuration, not aspirations.

`POST /v1/demo/runs` accepts `{"app_id":"document-demo"}` and returns HTTP 201 with `demo_run_id`, `app_id`, `created_at`, `mode`, and `max_requests:10`. Only one run may control the shared live alias at a time. Run isolation protects records; it does not create isolated AWS infrastructure. Enforce a live-app operation lock server-side. Cleanup releases the run only when no mutation is active.

## 2. Request submission

`POST /v1/demo/requests`:

```json
{
  "demo_run_id": "run_01",
  "document": {
    "fixture_id": "invoice-example-a",
    "document_type": "invoice",
    "page_count": 2
  }
}
```

`fixture_id` is a shipped allowlist enum, `document_type` is an enum, and page_count is bounded 1–10. No upload/URL fetching is needed. Canonicalize and store the input hash; assign the request ID server-side. Persist the inbox and processing operation before dispatch.

```json
{
  "resource_id": "docreq_01",
  "request_id": "docreq_01",
  "operation_id": "op_process_01",
  "status": "accepted",
  "status_url": "/v1/operations/op_process_01"
}
```

The processing dispatcher invokes the registered alias with the stored ID and data. It captures actual `FunctionError`/business failure, maintains request status, and emits a deduplicated incident event. Persist the source failure even if incident delivery is retried later.

Request status is `accepted`, `processing`, `failed`, `completed`, or `needs_attention`. A GET returns payload hash, attempts, error code if present, and verified result reference. A count includes only persisted requests in the authorized run.

## 3. Incident intake and investigations

`POST /v1/incidents` accepts a normalized event, not a forged raw AWS event:

```json
{
  "source_event_id": "evt_failure_01",
  "source": "incidentpilot.demo-dispatcher",
  "app_id": "document-demo",
  "demo_run_id": "run_01",
  "observed_at": "2026-09-13T20:00:00Z",
  "failure_kind": "processor_invocation_failed",
  "request_ids": ["docreq_01"],
  "evidence_ref": "failure_obs_01"
}
```

Allow only registered sources. Confirm referenced requests and observations belong to the app/run. Attach near-simultaneous failures to the active incident for the same app and observed deployment fingerprint; source event ID prevents exact redelivery duplicates. Never merge across workspace or run. The API returns the common 202 envelope with `resource_id` and optional `incident_id` set to the incident ID, plus the investigation `operation_id` and its `status_url`. If the incident is already investigating, attach evidence and return the existing operation without starting an uncontrolled second run.

P1 EventBridge maps an authenticated application failure event into this same domain operation. A manually triggered investigation is still allowed; label the trigger honestly. Late evidence may request a new investigation after the current one finishes.

`POST .../investigations` accepts `{"expected_incident_version":3}`. Use a conditional update; 409 on mismatch. Reject concurrent investigation or mutation for the same incident. A new investigation invalidates prior unexecuted plans and approvals; retain them for history. It returns the common 202 envelope with incident `resource_id`, stable investigation `operation_id`, and corresponding `status_url`.

`GET .../{incident_id}` returns:

```json
{
  "incident_id": "inc_01",
  "version": 4,
  "app_id": "document-demo",
  "demo_run_id": "run_01",
  "state": "awaiting_approval",
  "mode": "aws_live",
  "impact": {"failed_requests": 3, "completed_requests": 0},
  "diagnosis": {
    "assessment": "supported",
    "summary": "The deployed version targets a different results table.",
    "evidence_ids": ["ev_error", "ev_release"],
    "unknowns": []
  },
  "active_plan_id": "plan_01",
  "active_operation_id": null,
  "updated_at": "2026-09-13T20:01:00Z"
}
```

Return full hypothesis detail from a nested diagnosis field or a documented expansion. Never trim away unknowns merely to simplify the UI.

## 4. Plan, approval, and execution

The plan is generated by server policy from evidence and a deployment manifest. GET plan output includes this execution payload, presentation text, `plan_digest`, and `created_at`. Example IDs/revisions below are placeholders:

```json
{
  "schema_version": "1.0",
  "plan_id": "plan_01",
  "incident_id": "inc_01",
  "workspace_id": "workspace_demo",
  "app_id": "document-demo",
  "account_id": "DEMO_ACCOUNT_FROM_CONFIG",
  "region": "DEPLOYMENT_REGION",
  "action": "rollback_demo_alias",
  "function_name": "REGISTERED_FUNCTION",
  "alias_name": "live",
  "from_version": "7",
  "to_version": "6",
  "expected_alias_revision_id": "ACTUAL_REVISION_FROM_AWS",
  "routing": "unweighted",
  "good_version_fingerprint": "sha256:DEPLOYED_CONFIG_HASH",
  "verification_profile": "document-result-v1",
  "replay_requests": [{"request_id": "docreq_01", "payload_sha256": "ACTUAL_HASH"}],
  "evidence_ids": ["ev_error", "ev_release"],
  "expires_at": "2026-09-13T20:11:00Z"
}
```

An empty replay list means rollback and verification only. The UI cannot add IDs to an approved plan. Plan expiry defaults to ten minutes as an application policy. Bind all execution fields and replay input hashes into canonical bytes; compute the digest on the server. Keep evidence IDs and plan policy version with it for traceability.

`POST .../decisions`:

```json
{
  "plan_id": "plan_01",
  "plan_digest": "sha256:ACTUAL_PLAN_DIGEST",
  "expected_incident_version": 4,
  "decision": "approve"
}
```

Decision enum is `approve|reject`. The authenticated identity supplies actor ID. Approval transaction checks incident version, plan validity/digest, role, expiry, and absence of active execution. It persists the decision plus execution outbox operation atomically. Approval returns the common 202 envelope with incident `resource_id`, execution `operation_id`, its `status_url`, and additional `approval_id`; rejection returns 200 with approval ID and incident state. A second contradictory decision is 409. Retrying the same request/key retrieves the original response.

Do not hold an HTTP request open through investigation, rollback, or replay. The worker independently checks approval and live state immediately before execution. Operations progress through `queued`, `running`, `succeeded`, `failed`, or `needs_attention`, with phases `validate`, `apply`, `verify`, `replay`, `receipt`.

Enforce unique operation claim, lease expiry, and reconciliation after ambiguous AWS responses. Mark `succeeded` only when the operation's defined checks pass; maintain incident outcome separately when backlog work is unresolved. Incident state `service_restored_pending_requests` means health checks passed but affected requests remain unresolved; its receipt is partial. Expiry blocks the start of a new mutation, not reconciliation of one already attempted. A replay batch must start before approval expiry and may finish its exact IDs within a five-minute execution deadline; changes or deadline breaches stop further replay and produce a partial outcome.

## 5. Events and evidence

`GET .../events?after_sequence=0&limit=100`:

```json
{
  "items": [{
    "sequence": 8,
    "event_id": "event_08",
    "type": "investigation.tool_completed",
    "occurred_at": "2026-09-13T20:00:20Z",
    "data": {"tool_name": "get_release_diff", "duration_ms": 180, "evidence_ids": ["ev_release"]}
  }],
  "last_sequence": 8,
  "has_more": false
}
```

Other event types include incident.detected, investigation.started, investigation.completed, plan.created, plan.invalidated, decision.recorded, execution.started, execution.reconciled, verification.completed, request.reconciled, and receipt.created. All persisted event payloads are sanitized. Do not expose model hidden reasoning.

Evidence GET returns `evidence_id`, `incident_id`, `run_id`, `source_type`, `source_ref`, `observed_at`, `retrieved_at`, `payload_sha256`, `payload`, `truncated`, and warnings. Evidence content is immutable; corrections create a new record. Source references resolve through registered mappings, not arbitrary remote fetches.

## 6. Receipt

`GET .../receipt?format=json` (default) or `format=markdown`. Return 409 `RECEIPT_NOT_READY` if no receipt exists. Partial/failed receipts are valid and must state their outcome. Send an appropriate content type for Markdown and a safe download filename.

```json
{
  "schema_version": "1.0",
  "receipt_id": "receipt_01",
  "incident_id": "inc_01",
  "mode": "aws_live",
  "outcome": "recovered",
  "plan_id": "plan_01",
  "approval_id": "approval_01",
  "execution_id": "op_execute_01",
  "applied_version": "6",
  "verification": [
    {"check": "alias_version", "passed": true, "evidence_id": "ev_alias_after"},
    {"check": "canary_result_and_hash", "passed": true, "evidence_id": "ev_canary"}
  ],
  "requests": [
    {"request_id": "docreq_01", "outcome": "completed", "result_id": "docreq_01"}
  ],
  "unresolved_request_ids": [],
  "timing_ms": {"investigation": 18000, "approval_wait": 6000, "service_recovery": 5000, "request_recovery": 2000},
  "usage": {"model_id": "CONFIGURED_MODEL", "input_tokens": null, "output_tokens": null, "estimated_cost_usd": null},
  "created_at": "2026-09-13T20:02:00Z"
}
```

All values above are illustrative, not measured results. Unknown measurements are null. Receipt request outcomes include `completed`, `already_completed`, `still_failed`, `not_replayed`, and `conflict`. Incident outcome is `recovered`, `partial`, or `needs_attention`. The receipt must remain partial when service checks pass but affected requests are unresolved.

## Contract tests Claude must create

Validate input/output schemas and generated OpenAPI references. Test authorization on every mutation; same-key/same-body and same-key/different-body behavior; stale incident version; mismatched digest; expiry; active-run locking; unknown evidence; cross-run access; paginated event ordering; duplicate queue execution; and partial recovery receipt truthfulness. Use live deployment tests separately from offline CI.
