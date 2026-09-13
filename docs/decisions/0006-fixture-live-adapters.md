# 0006. One gateway protocol, two implementations, and the mode is always visible

**Status:** accepted

## Context

The project had to be demonstrable before an AWS account existed, without
that becoming a lie about what had been demonstrated.

## Decision

`packages/aws/gateway.py` defines one protocol. `FixtureAwsGateway`
simulates the alias, both results tables, and the log group in SQLite;
`LiveAwsGateway` uses boto3. The choice is made once, at startup, in
`services/api/config.py`. No handler branches on mode to pick a backend.

Crucially, fixture mode runs the *same* `demo/workload/processor.py` the
real Lambda deploys, and verification performs the same checks against the
same result records. The simulation is of AWS's API surface, not of the
product's logic.

Mode is surfaced everywhere it could mislead: `/v1/capabilities` reports it,
the receipt carries it, the UI banners it.

## Consequences

Fixture runs are reproducible, free, and offline -- so tests and evals can
exercise failure paths (alias moved mid-approval, canary fails) that would
be awkward to stage against real AWS. A passing fixture run is still not
evidence that live AWS works, and BUILD_STATUS.md says so.
