# IncidentPilot

**A green dashboard doesn't tell you which customer requests were recovered.**

IncidentPilot investigates a failed AWS request-processing workflow, prepares
a bounded rollback, gets a human to approve that exact change, executes it,
independently verifies that recovery actually happened, and produces a
**recovery receipt** accounting for every affected customer request —
including the ones it could *not* recover.

Built for the Agents for Humans hackathon (Professional Agents), using the
Strands Agents SDK on AWS.

---

## The 90-second version

A small SaaS team deploys a config change to a document-processing Lambda.
The new version points `RESULTS_TABLE` at a table its execution role can't
reach. Customer requests start failing with `AccessDenied`.

1. **Requests fail.** Three document requests are persisted *before*
   processing, so a failure leaves recoverable work, not lost work.
2. **The agent investigates.** A single Strands agent calls five read-only
   evidence tools over MCP — incident context, bounded error logs, a
   deployed-version diff, affected-request status, and the versioned runbook
   — and returns a structured diagnosis citing evidence IDs.
3. **Application code builds the plan.** The agent *recommends*; a
   deterministic nine-condition policy decides, constructing an immutable
   plan from the trusted deployment manifest and freshly re-read evidence.
4. **A human approves that exact plan.** The approval binds the operator to
   a SHA-256 digest of every execution-relevant field. Approve a stale plan
   and the server refuses and explains what changed.
5. **A separate executor applies it.** Different process, different IAM
   identity. It re-checks the digest, expiry, and the *live* alias revision
   immediately before calling `UpdateAlias`, and stops with `STALE_PLAN`
   rather than overwriting someone else's deployment.
6. **Verification is independent of the mutation.** A successful API call is
   not recovery. A synthetic canary runs, its durable result is read back,
   its payload hash is compared, and `ExecutedVersion` is checked.
7. **Bounded replay, then the receipt.** Only the request IDs baked into the
   approved plan are replayed. The receipt states what was verified, what
   wasn't, and which requests remain unresolved.

> The whole point: if the canary fails, nothing turns green. If two of three
> requests recover, the receipt says `partial` and names the third.

---

## Run it — no AWS account needed

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"        # Linux/macOS: .venv/bin/pip

# The whole slice, narrated step by step
.venv/Scripts/python scripts/run_fixture_demo.py --reset

# The same thing driven through the real HTTP API, with the refusals asserted
.venv/Scripts/python scripts/smoke_api.py

# Tests
.venv/Scripts/python -m pytest -q
```

To use the API and UI:

```bash
.venv/Scripts/python scripts/run_api.py        # 127.0.0.1:8080
.venv/Scripts/python scripts/run_worker.py     # separate terminal
```

Fixture mode simulates the Lambda alias, both DynamoDB results tables, and
the CloudWatch log group in SQLite — while running the *same* processor code
the real Lambda deploys. It is labelled as simulated everywhere it surfaces:
`/v1/capabilities`, the receipt, and the UI banner.

---

## Status — read this before believing anything

**[`docs/BUILD_STATUS.md`](docs/BUILD_STATUS.md) is the honest ledger** of
what is implemented, tested, unexercised, and not built.

The short version, as of 2026-09-13:

- ✅ The full recovery slice works offline and is covered by **107 tests**,
  weighted toward the refusals (abstention on weak evidence, stale plan,
  expired approval, digest mismatch, failed canary, duplicate replay,
  partial recovery).
- ✅ All **15** endpoints in the supplied API contract are implemented;
  `scripts/export_openapi.py` generates the OpenAPI document from the
  running app and diffs it against the contract.
- ⚠️ **The LLM has never run.** No AWS credentials were available in the
  build environment, so diagnoses currently come from a deterministic
  decision table (`agent_mode: "stub"`) that implements the same policy. The
  Strands/Bedrock path is written but unexercised, and the product reports
  `stub` rather than implying otherwise.
- ⚠️ `LiveAwsGateway` (boto3) is written and unexercised.
- ❌ `infra/` (CDK + the three separately-scoped IAM roles) is **not yet
  written** — the main gap between this and a live AWS demonstration.

---

## Architecture

See [`docs/architecture.md`](docs/architecture.md) for the diagram and the
data flow.

```
apps/web/          React workbench (incident list · incident · timeline)
services/api/      FastAPI control API — persists decisions, enqueues work
services/worker/   Drains processing + investigation operations
services/executor/ The ONLY code path that mutates the alias
agent/             Strands investigator + deterministic stub
mcp_server/        Read-only evidence tools (MCP stdio)
packages/domain/   Entities, state machine, plan digest, plan policy
packages/storage/  SQLite (fixture/local) behind one interface
packages/aws/      AwsGateway protocol: fixture and live implementations
demo/              The processor Lambda + trusted manifests
contracts/         Generated OpenAPI + MCP tool schemas
```

**The boundaries that matter**

| Boundary | Enforced by |
|---|---|
| The agent can read evidence but cannot act | No mutating tool exists; the MCP process can't import the executor; read-only IAM role in live mode |
| The agent can't widen its own scope | Its process is bound to one incident ID at spawn; other IDs return `RESOURCE_OUT_OF_SCOPE` |
| An approval means one exact change | SHA-256 digest over canonical bytes of every execution-relevant field |
| The API can't mutate AWS | Separate executor process and IAM identity; the API only persists + enqueues |
| "Recovered" is decided by checks, not the model | Independent canary + durable result + payload hash + `ExecutedVersion` |
| Recovery claims stay honest | Unresolved requests stay visible; partial outcomes are first-class |

Design decisions are recorded in [`docs/decisions/`](docs/decisions/) —
including why it's one agent and not a swarm, why the fix is a rollback
rather than widening IAM, and why the alias rollback deliberately creates
infrastructure drift.

---

## Why an LLM at all?

Because the useful step is *synthesis across heterogeneous evidence*:
correlating an `AccessDenied` string in a log with a config diff between two
deployed versions and a runbook's eligibility rules, then saying which
hypotheses the evidence supports and which it contradicts. That is judgment
over unstructured text.

Everything downstream of that judgment is deterministic on purpose. The
model cannot construct a plan, approve one, choose a resource, or execute
anything. `AccessDenied` alone is explicitly *insufficient* — without a
corroborating configuration difference the system abstains and says what
evidence it would need.

---

## License

MIT — see [LICENSE](LICENSE).
