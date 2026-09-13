# 0001. One agent, not a swarm

**Status:** accepted

## Context

The interesting work in this product is *deciding whether the evidence
supports a mutation*. It is tempting to model that as several specialists --
a log analyst, a deployment comparer, a planner, a critic -- talking to each
other.

## Decision

Use exactly one Strands agent, whose entire job is: call bounded evidence
tools, then return a structured diagnosis. Every state transition, plan
construction, approval check, and mutation is deterministic application code.

## Consequences

The agent's blast radius is one Pydantic object. Adding a second agent would
add a coordination protocol, non-determinism in ordering, and more places
for a hallucinated conclusion to gain authority -- while the actual decision
(`packages/domain/plan_policy.py`) is a nine-condition boolean that must not
be probabilistic anyway.

The cost: synthesis quality rests on one prompt and one model call. If
diagnosis quality proves insufficient in evaluation, the answer is better
evidence tools, not more agents.
