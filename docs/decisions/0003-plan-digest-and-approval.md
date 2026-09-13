# 0003. An approval binds an actor to a digest, not to an intention

**Status:** accepted

## Context

"The operator approved the rollback" is too vague to execute safely. The
alias could move between display and click; a new investigation could
supersede the plan; a client could post a plan ID while displaying something
else.

## Decision

`packages/domain/plan.py` defines a canonical byte serialization over
exactly the execution-relevant fields (`_CANONICAL_FIELD_ORDER`), hashed to
`sha256:<hex>`. An `Approval` stores that digest, the authenticated actor,
and the plan's expiry. Approving requires sending back both the digest that
was displayed and the incident version that was loaded; either being stale
is a refusal, never a silent re-approval.

Traceability fields (`plan_id`, `evidence_ids`, `created_at`,
`policy_version`) are deliberately *outside* the digest: they can change
without invalidating consent because they cannot change what executes.

## Consequences

Changing the canonicalization changes every previously-issued digest, so it
requires a `schema_version` bump. The digest is verified twice -- at
approval and again by the executor immediately before mutation -- because
the useful question is not "was this approved?" but "is what I am about to
do still the thing that was approved?"
