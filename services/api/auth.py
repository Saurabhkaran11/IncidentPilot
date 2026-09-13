"""Operator authentication for the P0 local demo.

Two modes, deliberately not interchangeable (build brief section 7):

**fixture** -- a fixed development actor, accepted only when the request
arrives on loopback. No token. This is why fixture mode is safe to hand
someone as a walkthrough but is never the hosted configuration.

**aws_live** -- a cryptographically random bearer token minted at server
boot *after* an STS identity check, stored only as a hash, bound to that AWS
identity + workspace + this boot session, expiring in 30 minutes. A restart
invalidates every token. Printed once to the owner's terminal; never logged,
never placed in a URL.

Neither of these is hosted authentication. A hosted deployment must use
Cognito (or another configured IdP) and reject local tokens outright --
``assert_not_hosted`` is the guard that makes that failure loud rather than
silent. See docs/decisions/0009-local-operator-auth.md.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Request

from packages.domain.enums import ErrorCode
from packages.domain.errors import ApiError
from packages.storage.interface import ControlPlaneStore, OperatorTokenRecord
from services.api.config import Settings

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

#: Regenerated every process start, so a restart invalidates prior tokens
#: even if the database file survives.
BOOT_SESSION_ID = secrets.token_hex(16)


@dataclass(frozen=True)
class Principal:
    actor_id: str
    workspace_id: str
    #: "operator" may inspect, investigate, and decide. "service" is the
    #: internal dispatcher identity permitted to POST /v1/incidents.
    role: str


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def assert_not_hosted() -> None:
    """Refuse to start local dev-auth in anything that looks hosted."""
    if os.environ.get("INCIDENTPILOT_HOSTED") == "1":
        raise RuntimeError(
            "local operator tokens are disabled in hosted deployments; configure Cognito instead"
        )


def mint_operator_token(
    store: ControlPlaneStore, settings: Settings, *, aws_identity_arn: str, actor_id: str
) -> str:
    """Create one short-lived token. Returns the raw value exactly once."""
    assert_not_hosted()
    raw = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    store.put_operator_token(
        OperatorTokenRecord(
            token_hash=_hash_token(raw),
            actor_id=actor_id,
            workspace_id=settings.workspace_id,
            aws_identity_arn=aws_identity_arn,
            issued_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=settings.token_ttl_seconds)).isoformat(),
            boot_session_id=BOOT_SESSION_ID,
        )
    )
    return raw


def _is_loopback(request: Request) -> bool:
    client = request.client
    return client is not None and client.host in _LOOPBACK_HOSTS


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def authenticate(request: Request, store: ControlPlaneStore, settings: Settings) -> Principal:
    """Resolve the caller, or raise ``ApiError(UNAUTHENTICATED)``.

    Workspace and actor come from *verified identity plus server config* --
    never from the request body (API contract: "Derive workspace and actor
    from verified identity and server configuration").
    """
    if not _is_loopback(request):
        # Both local modes are loopback-only by construction. A non-loopback
        # caller means someone exposed this process; fail closed.
        raise ApiError(ErrorCode.FORBIDDEN, "this deployment only accepts loopback connections")

    if not settings.is_live:
        return Principal(actor_id=settings.dev_actor_id, workspace_id=settings.workspace_id, role="operator")

    raw = _bearer_token(request)
    if raw is None:
        raise ApiError(ErrorCode.UNAUTHENTICATED, "operator bearer token required")

    record = store.get_operator_token(_hash_token(raw))
    if record is None or not hmac.compare_digest(record.token_hash, _hash_token(raw)):
        raise ApiError(ErrorCode.UNAUTHENTICATED, "invalid operator token")
    if record.boot_session_id != BOOT_SESSION_ID:
        raise ApiError(ErrorCode.UNAUTHENTICATED, "token was issued by a previous server session")
    if datetime.fromisoformat(record.expires_at) < datetime.now(UTC):
        raise ApiError(ErrorCode.UNAUTHENTICATED, "operator token expired")

    return Principal(actor_id=record.actor_id, workspace_id=record.workspace_id, role="operator")


def verify_aws_identity() -> str:
    """STS check performed before minting a live-mode token."""
    import boto3

    identity = boto3.client("sts").get_caller_identity()
    return identity["Arn"]
