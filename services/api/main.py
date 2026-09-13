"""FastAPI application factory.

Serves the control API (``services/api/routes.py``) and, in a packaged
build, the compiled React workbench. Deliberately does NOT run investigation
or recovery work in-process: those are durable operations drained by
``services/worker`` and ``services/executor`` (build brief section 7, "Do
not rely on FastAPI in-process background tasks for durable execution").

Run locally:

    python scripts/run_api.py          # fixture mode, loopback only
    python scripts/run_worker.py       # the other half -- drains operations
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from packages.domain.enums import ErrorCode, Mode
from packages.domain.errors import ApiError, ErrorDetail, ErrorEnvelope
from services.api import routes
from services.api.auth import mint_operator_token, verify_aws_identity
from services.api.config import get_gateway, get_settings, get_store

logger = logging.getLogger("incidentpilot.api")


def _request_id(request: Request) -> str:
    existing = getattr(request.state, "request_id", None)
    if existing:
        return existing
    generated = f"req_{uuid.uuid4().hex[:12]}"
    request.state.request_id = generated
    return generated


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup work that must complete before the first request.

    Revoking tokens here is what makes "a restart invalidates the operator
    token" true even though the SQLite file survives the restart.
    """
    settings = get_settings()
    store = get_store()
    get_gateway()
    store.revoke_all_operator_tokens()

    if settings.mode is Mode.FIXTURE and store.get_app_config(settings.app_id) is None:
        from scripts.seed_fixture import seed

        seed(settings.data_dir)
        logger.info("seeded fixture data in %s", settings.data_dir)

    if settings.is_live:
        arn = verify_aws_identity()
        token = mint_operator_token(store, settings, aws_identity_arn=arn, actor_id=arn.rsplit("/", 1)[-1])
        # Printed once, to this terminal only. Never logged, never in a URL.
        print("\n  IncidentPilot operator token (valid 30 minutes, this session only):", file=sys.stderr)
        print(f"  {token}\n", file=sys.stderr)

    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="IncidentPilot control API",
        version="1.0.0",
        description=(
            "Investigate a failed AWS request-processing workflow, approve a bounded "
            "Lambda alias rollback, and verify recovery."
        ),
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # Exactly one allowed origin -- the local UI. Not "*": a wildcard here
    # would let any page a browser visits drive an approval.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.ui_origin],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )

    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        envelope = exc.to_envelope(_request_id(request))
        return JSONResponse(status_code=exc.status_code, content=envelope.model_dump(mode="json"))

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Normalize FastAPI's default 422 body into the common envelope so
        # clients parse one error shape (API contract, error section).
        envelope = ErrorEnvelope(
            error=ErrorDetail(
                code=ErrorCode.VALIDATION_ERROR,
                message="request body failed schema validation",
                retryable=False,
                request_id=_request_id(request),
                details={"errors": exc.errors()[:10]},
            )
        )
        return JSONResponse(status_code=422, content=envelope.model_dump(mode="json"))

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        # Never leak a stack trace or an AWS error payload to the client.
        logger.exception("unhandled error on %s", request.url.path)
        envelope = ErrorEnvelope(
            error=ErrorDetail(
                code=ErrorCode.INTERNAL_ERROR,
                message="internal error",
                retryable=False,
                request_id=_request_id(request),
            )
        )
        return JSONResponse(status_code=500, content=envelope.model_dump(mode="json"))

    app.include_router(routes.router)

    return app


app = create_app()
