# Infrastructure

CDK (TypeScript) for the bounded demo environment: one processor Lambda with
an alias, two DynamoDB results tables, a short-retention log group, and three
separately-scoped runtime roles.

**Status: synthesizes cleanly; never deployed.** No AWS account was available
while this was built — see [`../docs/BUILD_STATUS.md`](../docs/BUILD_STATUS.md).

## What it creates

| Resource | Name | Note |
|---|---|---|
| Lambda | `incidentpilot-demo-processor` | Runs `demo/workload/processor.py` — the same module fixture mode runs in-process |
| Alias | `live` | The only thing the executor may move |
| DynamoDB | `incidentpilot-demo-results` | The expected results table |
| DynamoDB | `incidentpilot-demo-results-quarantine` | Exists; the processor role cannot reach it. This is the fault. |
| Log group | `/aws/lambda/incidentpilot-demo-processor` | 3-day retention — a demo, not an audit trail |
| IAM roles | processor / investigator / executor | See [`../docs/permissions.md`](../docs/permissions.md) |

No VPC, no NAT gateway, no cluster, no always-on database. Each would cost
more than the workload it supported.

## Deploy

```bash
npm install
npx cdk bootstrap          # first time in this account/region
npx cdk synth              # inspect before creating anything billable
npx cdk deploy

cd .. && python scripts/deploy_demo_workload.py --region <your-region>
```

`deploy_demo_workload.py` publishes version **G** (expected table), proves it
healthy with a canary *before* recording it as the known-good rollback
target, publishes version **B** (quarantine table), and writes the real
published version numbers into `demo/manifests/`. It leaves the alias on G —
breaking the demo is a separate explicit step:

```bash
INCIDENTPILOT_MODE=aws_live python scripts/inject_fault.py
```

## Cost

Everything here is pay-per-request with no idle cost: Lambda invocations,
two on-demand DynamoDB tables, and a few MB of logs expiring in 3 days. A
full demo run is a few dozen invocations and a handful of item writes.

The variable worth watching is **Bedrock**, which is billed per token and is
not created by this stack. Record actual usage from the receipt rather than
estimating — `usage` is `null` when it wasn't measured.

Set a budget alert before deploying. Note that an AWS Budget is a
*notification*, not a spending cap.

## Teardown

```bash
npx cdk destroy
```

Both tables use `RemovalPolicy.DESTROY` because they hold fixture documents,
not customer data. Confirm that is still true before reusing this stack for
anything real.

## Drift warning

An approved rollback changes where the `live` alias points. This stack also
manages that alias, so a later `cdk deploy` will move it back. That drift is
deliberate and documented in
[`../docs/decisions/0012-iac-drift.md`](../docs/decisions/0012-iac-drift.md):
during an incident the deployment manifest is the recovery authority, and a
redeploy afterwards must reconcile alias state.
