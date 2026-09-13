# 0012. The alias rollback deliberately creates IaC drift, and the receipt says so

**Status:** accepted

## Context

The demo alias is created by CDK. An approved rollback changes where that
alias points. The next `cdk deploy` would happily point it back at the
faulty version.

## Decision

Accept the drift, deliberately and visibly. During an incident the
deployment manifest is the recovery authority, not the IaC template. The
receipt records the applied version so the drift is documented rather than
discovered, and can propose the corresponding source-configuration
correction.

Automatically opening a pull request to fix the source configuration is
explicitly later work. It is not a prerequisite for claiming recovery,
because recovery is a statement about the running system and verified
request results, not about the repository.

## Consequences

A redeploy after an incident must reconcile alias state -- an operational
fact this project documents rather than pretends away. The alternative,
having the recovery path edit infrastructure code, would put a mutation with
a much larger blast radius behind the same approval.
