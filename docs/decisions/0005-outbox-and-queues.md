# 0005. Persist the job before acknowledging the command

**Status:** accepted

## Context

The failure mode that loses approved work: return 202, then die before the
work is durable. The operator believes recovery is running; nothing is.

## Decision

Every asynchronous command writes its operation row *before* responding.
Approval writes the `Approval` and the execution `Operation` together, then
returns. Workers find queued operations in storage, so a crash between
acknowledgement and execution loses nothing.

Locally, `services/worker/loop.py` polls. In the hosted design each kind
becomes its own SQS queue with its own worker Lambda and its own role --
separate queues specifically so the investigation path and the execution
path do not share an identity.

## Consequences

SQS delivers at least once, so both queue handling and business effects must
be idempotent: operations are claimed with conditional leases, and the one
business effect (the result insert) is a conditional put keyed by request
ID. This project does not claim exactly-once delivery; it claims exactly-one
durable result per request, which is the property that actually matters.

The local loop is single-worker by assumption -- see docs/BUILD_STATUS.md.
