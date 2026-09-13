# 0007. The live demonstration is blocked on account setup, and we say so

**Status:** accepted

## Context

No AWS credentials are configured in the environment where this was built
(`aws sts get-caller-identity` fails with `NoCredentials`). The build brief
anticipated this: continue building the fixture version and deployable
infrastructure, then identify the exact missing setup.

## Decision

Build everything that does not require an account; write the live path
against current documented APIs; label it unexercised; and enumerate the
precise blocking steps in docs/BUILD_STATUS.md rather than implying a live
run happened.

## Consequences

The repository's P0 claim is "a complete, verifiable recovery slice that
runs offline, plus a live code path that has not been run" -- which is
defensible -- rather than "a working AWS demo", which would not be.

The remaining distance is small and named: an account, Bedrock model access,
and the `infra/` CDK app with three separately-scoped roles.
