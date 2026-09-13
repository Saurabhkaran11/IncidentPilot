# 0009. A local operator token is a demo mechanism, not authentication

**Status:** accepted

## Context

P0 is a single operator on their own machine driving a real AWS mutation.
That needs *some* gate, but standing up Cognito for it would be scope no one
asked for.

## Decision

Two modes, deliberately not interchangeable. Fixture mode uses a fixed dev
actor, accepted only on loopback. Live mode mints a random token at boot
*after* an STS identity check, stores only its SHA-256 hash, binds it to
that identity and the boot session, expires it in 30 minutes, and prints it
once to the owner's terminal. A restart invalidates it. The API binds to
127.0.0.1 and allows exactly one CORS origin.

`services/api/auth.py:assert_not_hosted` refuses to mint tokens when
`INCIDENTPILOT_HOSTED=1`, so shipping this to a hosted environment fails
loudly instead of silently becoming the auth story.

## Consequences

Good enough for a local single-operator demo, and clearly labelled as not
hosted authentication. Hosted mode must use Cognito and reject these tokens.
The loopback check is strict enough that even test clients must present a
real loopback address rather than the check being relaxed for tests.
