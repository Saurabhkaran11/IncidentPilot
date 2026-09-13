"""Opaque ID generation.

IDs are stable, prefixed, URL-safe strings. They are identifiers, never
bearer credentials -- callers must still be authenticated/authorized
separately (see docs/decisions/0003-plan-digest-and-approval.md).

Prefixes in use: ``inc`` incident, ``ev`` evidence, ``diag`` diagnosis,
``plan`` recovery plan, ``approval``, ``op`` operation/execution,
``docreq`` demo request, ``receipt``, ``run`` demo run, ``event``.
"""

from __future__ import annotations

import secrets

_ALPHABET = "abcdefghijkmnopqrstuvwxyz23456789"  # no 0/1/l/o ambiguity


def new_id(prefix: str) -> str:
    """Generate a new opaque ID like ``inc_7xk2m9...``."""
    suffix = "".join(secrets.choice(_ALPHABET) for _ in range(20))
    return f"{prefix}_{suffix}"
