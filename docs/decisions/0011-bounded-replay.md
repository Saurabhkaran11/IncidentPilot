# 0011. Replay only the request IDs baked into the approved plan

**Status:** accepted

## Context

After a rollback the service is healthy, but the requests that failed during
the outage are still unprocessed. Replaying "everything currently failed" is
the obvious implementation and the wrong one: the set can grow between
approval and execution, so the operator would be consenting to an unbounded
batch.

## Decision

The plan carries an explicit list of `(request_id, payload_sha256)` pairs,
at most ten, and those pairs are inside the digest. The executor replays
exactly that list. It re-checks the deployed version before each item, stops
on a version change or the five-minute execution deadline, and reports
anything it did not attempt as `not_replayed` rather than dropping it.

Duplicate protection is the processor's conditional insert: a matching
payload hash means already-completed and returns the existing result; a
differing hash for the same request ID is a conflict requiring attention,
never an overwrite.

## Consequences

The approval means precisely what it said. Partial outcomes are first-class:
if some requests remain unresolved the incident becomes
`service_restored_pending_requests` and the receipt is `partial` with the
outstanding IDs listed -- the product's central promise.
