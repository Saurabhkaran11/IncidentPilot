# 0008. Model ID is configuration, and usage is recorded, not assumed

**Status:** accepted

## Context

Bedrock model availability varies by account and region, and model IDs
change. Hard-coding one guarantees a broken demo on someone else's account.

## Decision

`INCIDENTPILOT_BEDROCK_MODEL_ID` selects the model;
`agent/investigator.py:DEFAULT_MODEL_ID` is a starting point to be verified
against the access actually granted. The resolved model ID and prompt
version are recorded on every `Diagnosis` and surfaced in the receipt.

Token usage is read best-effort across the shapes Strands has exposed it in,
and reported as `null` when unavailable -- never estimated and presented as
measured.

## Consequences

Evaluation results are reproducible because model and prompt version travel
with each diagnosis. Cost claims stay honest: unknown costs remain unknown
rather than becoming zero.
