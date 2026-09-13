"""The demo Lambda handler: source of truth for both fixture and live mode.

This module IS the real ``lambda_function.py`` deployed to AWS (see
``infra/`` and ``scripts/deploy_demo_workload.py``) -- not a separate mock.
Fixture mode invokes ``handler()`` in-process instead of over the network;
live mode is this exact file zipped and published as versions G and B with
different ``RESULTS_TABLE`` environment variables (build brief section 5).

Business logic, on purpose, is tiny: given a request ID and fixture document
metadata, write one deterministic result row to ``RESULTS_TABLE`` with a
conditional (idempotent) put. Everything interesting in this project is
about *investigating and recovering* this one side effect, not the side
effect itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Protocol


class ResultsTableUnavailable(Exception):
    """Raised when the configured RESULTS_TABLE cannot be written to.

    In live mode this wraps a real ``botocore.exceptions.ClientError``
    (e.g. AccessDeniedException because version B points at a table the
    processor role cannot reach). The handler never swallows this -- it is
    exactly the failure the investigator is supposed to find evidence of.
    """

    def __init__(self, table_ref: str, cause: str) -> None:
        self.table_ref = table_ref
        self.cause = cause
        super().__init__(f"results table {table_ref!r} unavailable: {cause}")


@dataclass(frozen=True)
class PutOutcome:
    inserted: bool
    already_completed: bool
    conflict: bool
    result_id: str
    payload_sha256: str


class ResultsTable(Protocol):
    """Minimal conditional-write contract the handler depends on.

    ``demo/workload/fixture_results_table.py`` implements this in-memory;
    live mode implements it with ``boto3`` DynamoDB
    ``put_item(ConditionExpression="attribute_not_exists(request_id)")``.
    """

    def conditional_put(self, request_id: str, payload_sha256: str, item: dict[str, Any]) -> PutOutcome: ...

    def get(self, request_id: str) -> dict[str, Any] | None: ...


def canonicalize_input(request_id: str, document: dict[str, Any]) -> dict[str, Any]:
    """The exact structure whose hash is the request's identity.

    Both the API (when it first persists a submitted request) and this
    handler must compute the identical hash from identical input, or
    duplicate-delivery detection breaks. Keep key order and field set fixed.
    """
    return {
        "request_id": request_id,
        "fixture_id": document["fixture_id"],
        "document_type": document["document_type"],
        "page_count": document["page_count"],
    }


def payload_sha256(canonical_input: dict[str, Any]) -> str:
    encoded = json.dumps(canonical_input, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def handle(
    event: dict[str, Any],
    *,
    results_table: ResultsTable,
    results_table_ref: str,
) -> dict[str, Any]:
    """Process one request. Raises ``ResultsTableUnavailable`` on failure.

    ``event`` shape: ``{"request_id": str, "document": {...}}``. This
    matches what ``services/worker`` sends whether it is invoking the
    fixture handler in-process or a real Lambda alias over the network --
    the payload crossing the wire is identical either way.
    """
    request_id = event["request_id"]
    document = event["document"]
    canonical = canonicalize_input(request_id, document)
    digest = payload_sha256(canonical)

    item = {
        "request_id": request_id,
        "payload_sha256": digest,
        "document_type": document["document_type"],
        "page_count": document["page_count"],
        "processed_at": event.get("_now") or _now_iso(),
        "results_table_ref": results_table_ref,
    }

    outcome = results_table.conditional_put(request_id, digest, item)
    return {
        "request_id": request_id,
        "result_id": request_id,
        "payload_sha256": digest,
        "already_completed": outcome.already_completed,
        "conflict": outcome.conflict,
    }


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # pragma: no cover
    """Real AWS entrypoint (``handler`` in ``infra`` CDK Lambda config).

    Reads ``RESULTS_TABLE`` and ``SCHEMA_VERSION`` from the environment --
    these are the *only* two env vars this function reads, matching the
    MCP evidence tool's allowlist (get_release_diff ``changes[].field``).
    Deliberately does not import boto3 at module scope beyond what's needed
    so a bad ``RESULTS_TABLE`` fails at call time (the failure the whole
    demo investigates), not at import time.
    """
    import boto3

    table_ref = os.environ["RESULTS_TABLE"]
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(table_ref)

    class _LiveResultsTable:
        def conditional_put(self, request_id: str, digest: str, item: dict[str, Any]) -> PutOutcome:
            from botocore.exceptions import ClientError

            try:
                table.put_item(
                    Item=item,
                    ConditionExpression="attribute_not_exists(request_id)",
                )
                return PutOutcome(True, False, False, request_id, digest)
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code == "ConditionalCheckFailedException":
                    existing = table.get_item(Key={"request_id": request_id}).get("Item")
                    if existing and existing.get("payload_sha256") == digest:
                        return PutOutcome(False, True, False, request_id, digest)
                    return PutOutcome(False, False, True, request_id, digest)
                raise ResultsTableUnavailable(table_ref, f"{code}: {exc.response['Error']['Message']}") from exc

        def get(self, request_id: str) -> dict[str, Any] | None:
            return table.get_item(Key={"request_id": request_id}).get("Item")

    try:
        result = handle(event, results_table=_LiveResultsTable(), results_table_ref=table_ref)
        return {"statusCode": 200, "body": result}
    except ResultsTableUnavailable:
        # Re-raise so Lambda records a real FunctionError -- the investigator
        # and the dispatcher both depend on seeing an actual invocation
        # failure, not a 200 with an error string buried in the body.
        raise
