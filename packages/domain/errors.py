"""The common API error envelope and the exception that carries it.

``services/api`` installs one FastAPI exception handler for ``ApiError`` so
every route returns the same JSON shape (see docs/specs/IncidentPilot-API-Contract.md
"Common error envelope") instead of each route hand-rolling error JSON.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from packages.domain.enums import ErrorCode

_STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.STALE_INCIDENT_VERSION: 409,
    ErrorCode.STALE_PLAN: 409,
    ErrorCode.PLAN_EXPIRED: 409,
    ErrorCode.PLAN_DIGEST_MISMATCH: 409,
    ErrorCode.DECISION_CONFLICT: 409,
    ErrorCode.RUN_LOCKED: 409,
    ErrorCode.IDEMPOTENCY_KEY_REQUIRED: 400,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.RECEIPT_NOT_READY: 409,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.DEPENDENCY_UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}

#: Codes safe to retry with no change to the request.
_RETRYABLE: frozenset[ErrorCode] = frozenset({ErrorCode.RATE_LIMITED, ErrorCode.DEPENDENCY_UNAVAILABLE})


class ErrorDetail(BaseModel):
    code: ErrorCode
    message: str
    retryable: bool
    request_id: str
    details: dict[str, Any] | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorDetail


class ApiError(Exception):
    """Raise this from any route/service function; the handler does the rest."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.details = details
        self.retryable = (code in _RETRYABLE) if retryable is None else retryable
        self.status_code = _STATUS_BY_CODE[code]
        super().__init__(message)

    def to_envelope(self, request_id: str) -> ErrorEnvelope:
        return ErrorEnvelope(
            error=ErrorDetail(
                code=self.code,
                message=self.message,
                retryable=self.retryable,
                request_id=request_id,
                details=self.details,
            )
        )
