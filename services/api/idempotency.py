"""``Idempotency-Key`` handling for mutating POST routes.

API contract: keys are 8-128 chars, scoped to workspace + principal +
method + route. The same key with the same body replays the original
response; the same key with a *different* body is a 409. This is what makes
a double-clicked Approve button harmless without the UI having to guess.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from packages.domain.enums import ErrorCode
from packages.domain.errors import ApiError
from packages.storage.interface import ConcurrencyConflict, ControlPlaneStore, IdempotencyRecord
from services.api.auth import Principal

MIN_KEY_LENGTH = 8
MAX_KEY_LENGTH = 128


@dataclass(frozen=True)
class ReplayedResponse:
    status_code: int
    body: dict[str, Any]


def _scope_key(key: str, principal: Principal, method: str, route: str) -> str:
    return f"{principal.workspace_id}|{principal.actor_id}|{method}|{route}|{key}"


def _request_hash(body: Any) -> str:
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def require_key(raw_key: str | None) -> str:
    if raw_key is None:
        raise ApiError(ErrorCode.IDEMPOTENCY_KEY_REQUIRED, "Idempotency-Key header is required")
    key = raw_key.strip()
    if not (MIN_KEY_LENGTH <= len(key) <= MAX_KEY_LENGTH):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"Idempotency-Key must be {MIN_KEY_LENGTH}-{MAX_KEY_LENGTH} characters",
        )
    return key


def check_replay(
    store: ControlPlaneStore, key: str, principal: Principal, method: str, route: str, body: Any
) -> ReplayedResponse | None:
    """Return the original response if this exact call already happened."""
    record = store.get_idempotency_record(_scope_key(key, principal, method, route))
    if record is None:
        return None
    if record.request_hash != _request_hash(body):
        raise ApiError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            "this Idempotency-Key was already used with a different request body",
            details={"resource_id": record.resource_id},
        )
    return ReplayedResponse(status_code=record.status_code, body=json.loads(record.response_json))


def record(
    store: ControlPlaneStore,
    key: str,
    principal: Principal,
    method: str,
    route: str,
    body: Any,
    *,
    resource_id: str,
    response: dict[str, Any],
    status_code: int,
) -> None:
    try:
        store.put_idempotency_record(
            IdempotencyRecord(
                scope_key=_scope_key(key, principal, method, route),
                request_hash=_request_hash(body),
                resource_id=resource_id,
                response_json=json.dumps(response, default=str),
                status_code=status_code,
            )
        )
    except ConcurrencyConflict:
        # Two concurrent identical calls raced; the first one's response
        # stands and the second caller gets it on retry.
        pass
