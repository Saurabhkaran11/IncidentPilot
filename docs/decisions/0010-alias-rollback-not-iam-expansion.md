# 0010. Roll back the deployment; never widen IAM to make an error go away

**Status:** accepted

## Context

The demo failure is `AccessDenied`: the deployed version points
`RESULTS_TABLE` at a table the processor role cannot reach. There are two
ways to make that error stop. One is to grant the role access to that table.

## Decision

The only remediation this system can perform is pointing the alias back at
its recorded known-good version. No IAM mutation is reachable from any code
path -- not by the agent, not by the executor, not by an API route.

The runbook makes this explicit: if the processor role *differs* from the
trusted manifest, that is an IAM change and the policy refuses to act
(`ROLE_MISMATCH`), rather than treating a permissions drift as something to
remediate automatically.

## Consequences

Granting access would "fix" the symptom by making the bad configuration
work, writing customer results into the wrong table -- silent data
misplacement instead of a loud failure. Rolling back restores the known-good
target. It also means a genuine permissions regression is out of scope and
surfaces as "needs a human", which is the correct answer.
