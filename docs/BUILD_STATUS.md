# Build status

Last updated: **2026-09-13 06:40 PT**.

This file is the project's honesty ledger. If something here says "not
exercised", it means exactly that: the code exists and may be correct, but
no one has watched it run. Nothing in this repository claims a capability
that has not been demonstrated.

**Hackathon timing.** Agents for Humans submission closes **2026-09-14
17:00 PT**; roughly **34 hours** remain as of the timestamp above. This is
before the deadline, so the project is still a live entry.

---

## What actually runs today

Two commands reproduce everything below on a clean machine with no AWS
account:

```bash
.venv/Scripts/python scripts/run_fixture_demo.py --reset   # the whole slice, narrated
.venv/Scripts/python scripts/smoke_api.py                  # the same thing over HTTP
```

Both currently pass. `pytest -q` is **107 passed**.

### Implemented and tested (fixture mode)

| Capability | Where | Evidence it works |
|---|---|---|
| Incident lifecycle state machine | `packages/domain/state_machine.py` | 32 policy tests incl. every brief edge + illegal edges |
| Plan canonicalization and SHA-256 digest | `packages/domain/plan.py` | 20 tests: all 15 execution-relevant fields change the digest; 5 traceability fields don't |
| Deterministic plan-construction policy | `packages/domain/plan_policy.py` | all 9 rejection reason codes + good path |
| Read-only MCP evidence tools (5) | `mcp_server/tools.py` | 22 contract tests against the real JSON Schemas |
| MCP stdio server | `mcp_server/server.py` | starts and lists tools; see caveat below |
| Deterministic investigator (`agent_mode=stub`) | `agent/stub_investigator.py` | abstention + supported-diagnosis integration tests |
| Approval binding (digest + actor + expiry) | `services/api/routes.py` | tampered digest, stale version, expired plan all refused |
| Idempotency-Key handling | `services/api/idempotency.py` | replay returns original; different body → 409 |
| Incident intake dedup (2 layers) | `services/intake.py` | duplicate source event → same incident |
| Recovery executor (rollback + reconcile) | `services/executor/rollback_executor.py` | stale-revision refusal asserted not to overwrite |
| Independent verification (alias + canary + result hash) | same | failed canary → `needs_attention`, never `recovered` |
| Bounded replay with duplicate protection | `services/executor/replay.py` | matching hash → one durable result; differing hash → conflict |
| Recovery receipt (JSON + Markdown) | `services/api/receipt_markdown.py` | partial-recovery truthfulness test |
| Control API, all 15 contract endpoints | `services/api/` | `scripts/export_openapi.py` diffs generated OpenAPI vs the contract |
| SQLite control-plane store | `packages/storage/sqlite_store.py` | exercised by every integration test |
| Simulated AWS (alias, results tables, logs) | `packages/aws/fixture_gateway.py` | exercised by every integration test |

### Implemented but NOT exercised

These are written, type-check, and import cleanly. None has been run
against the real service. Do not describe them as working.

| Thing | Why not exercised | What it needs |
|---|---|---|
| `LiveAwsGateway` (boto3 Lambda/DynamoDB/CloudWatch) | no AWS credentials in this environment | an AWS account + `aws configure` |
| Strands + Bedrock investigator (`agent_mode=bedrock`) | no credentials, no Bedrock model access | Bedrock `InvokeModel` grant for the configured model ID |
| Strands consuming the MCP server over stdio | the five tools are tested in-process; the stdio round-trip through a real `MCPClient` has not been observed | a Bedrock-capable run, or a standalone MCP client smoke test |
| Live-mode operator token minting | requires `sts:GetCallerIdentity` | credentials |
| `demo/workload/processor.py:lambda_handler` | fixture mode calls `handle()` in-process; the AWS entrypoint wrapper itself is unrun | a deployed function |

### Not built

- `infra/` — CDK app and explicit runtime IAM roles. **This is the largest
  remaining P0 gap**; without it the live demo can't be provisioned reproducibly.
- DynamoDB control-plane adapter (hosted-live state).
- Frontend (`apps/web/`) — in progress at the time of writing.
- AgentCore Runtime deployment, EventBridge intake, hosted UI/Cognito/SQS (all P1).
- `evals/` — measured agent-evaluation results. The eval *scenarios* exist
  as integration tests; what's missing is repeated runs against a real model
  with published sample sizes, latency, and token usage.

---

## Known gaps and honest caveats

**The agent has never run.** Every diagnosis you can currently produce comes
from `agent/stub_investigator.py`, a deterministic decision table — not an
LLM. `/v1/capabilities` reports `agent_mode: "stub"` so the product never
implies otherwise. The Strands code path is written and the stub implements
the same decision policy, so the swap is a configuration change, but until
it runs against Bedrock that is a claim about design, not a measurement.

**Fixture mode is a simulation, and says so.** `FixtureAwsGateway`
simulates the Lambda alias, two DynamoDB results tables, and the CloudWatch
log group in SQLite. It runs the *same* `demo/workload/processor.py` logic
the real Lambda would, and verification performs the same checks — but a
passing fixture run is not evidence that live AWS works.

**Single-worker assumption.** `services/worker/loop.py` has no atomic claim
between "list queued" and "mark running". Two concurrent workers against one
SQLite file could double-process. The hosted design uses SQS visibility
timeouts for that guarantee; locally, run one worker.

**Dependency pin correction.** The research notes for this build suggested
`mcp>=2.2.0`, but `strands-agents 1.55.1` pins `mcp<2.2`. We follow the
SDK's constraint (`mcp>=1.23.0,<2.2`); resolved version is `mcp 2.1.1`.

**Fixture version numbers are not AWS version numbers.** The manifests use
`"6"` (good) and `"7"` (faulty) to match the illustrative example in the API
contract. Real published versions come from `publish-version` output.

---

## Exactly what is missing for a live AWS run

No secrets are needed in chat. The blocking setup, in order:

1. **An AWS account and region**, and a decision on a spend limit. Nothing
   here provisions billable resources yet.
2. **Local credentials** — `aws configure` (or SSO). Verify with
   `aws sts get-caller-identity`.
3. **Bedrock model access** for the model ID in
   `agent/investigator.py:DEFAULT_MODEL_ID`, granted in that region. Set
   `INCIDENTPILOT_BEDROCK_MODEL_ID` to whatever is actually enabled.
4. **`infra/` CDK app** — not yet written. It must create: the processor
   Lambda with two published versions and an alias, the expected results
   table, a second results table the processor role cannot access, the log
   group, and **separate IAM roles** for investigator (read-only),
   executor (`UpdateAlias` on one alias + invoke the canary), and processor.
5. `scripts/deploy_demo_workload.py` — also not written; it must record the
   real published version numbers into `demo/manifests/`.

Until 4 and 5 exist, `INCIDENTPILOT_MODE=aws_live` will start and
authenticate but has no resources to act on.

---

## Next concrete step

Write `infra/` (CDK, TypeScript) with the three separately-scoped runtime
roles, then `scripts/deploy_demo_workload.py` to publish versions G and B
and write their real numbers into `demo/manifests/`. That is the whole
remaining distance to a live P0 demonstration.
