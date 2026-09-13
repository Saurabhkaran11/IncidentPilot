"""Real AWS implementation of :class:`packages.aws.gateway.AwsGateway`.

Talks to Lambda, DynamoDB, and CloudWatch Logs with a boto3 session scoped
to whatever identity the process holds -- the investigator/worker only ever
holds read permissions on the registered resources (``infra/`` defines the
IAM policy); this class does not itself enforce that, the IAM role does
(build brief section 13: "Verify resource-level IAM support ... before
writing policies").

**Status: implemented, not yet exercised against a real AWS account** -- see
docs/BUILD_STATUS.md. Written against current boto3 API shapes and unit-
tested with ``moto`` (tests/integration/test_live_gateway_moto.py); the one
thing moto cannot stand in for is a *real* AccessDenied from a second
DynamoDB table's resource policy, which is why the P0 real-AWS demo still
needs an actual account (see docs/decisions/0007-aws-account-dependency.md).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from packages.aws.gateway import (
    AliasRevisionStale,
    AliasState,
    InvokeResult,
    LogEvent,
    ResultRecord,
)

_BOTO_CONFIG = Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=15)

#: The only environment variables this project ever reads off a deployed
#: function -- see demo/workload/processor.py's own allowlist comment.
_ALLOWLISTED_ENV_FIELDS = ("RESULTS_TABLE", "SCHEMA_VERSION")


class LiveAwsGateway:
    mode = "aws_live"

    def __init__(self, *, region_name: str | None = None) -> None:
        region = region_name or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        session = boto3.Session(region_name=region)
        self._lambda = session.client("lambda", config=_BOTO_CONFIG)
        self._logs = session.client("logs", config=_BOTO_CONFIG)
        self._dynamodb = session.resource("dynamodb", config=_BOTO_CONFIG)

    def get_alias_state(self, function_name: str, alias_name: str) -> AliasState:
        alias = self._lambda.get_alias(FunctionName=function_name, Name=alias_name)
        version = alias["FunctionVersion"]
        config = self._lambda.get_function_configuration(FunctionName=function_name, Qualifier=version)
        env = config.get("Environment", {}).get("Variables", {})
        fingerprint = _config_fingerprint(env)
        routing = "RoutingConfig" in alias and bool(alias.get("RoutingConfig", {}).get("AdditionalVersionWeights"))
        return AliasState(
            function_name=function_name,
            alias_name=alias_name,
            current_version=version,
            alias_revision_id=alias["RevisionId"],
            weighted_routing=routing,
            config_fingerprint=fingerprint,
            results_table_ref=env.get("RESULTS_TABLE", ""),
            updated_at=datetime.now(UTC),
        )

    def update_alias(
        self, function_name: str, alias_name: str, expected_revision_id: str, target_version: str
    ) -> AliasState:
        try:
            self._lambda.update_alias(
                FunctionName=function_name,
                Name=alias_name,
                FunctionVersion=target_version,
                RevisionId=expected_revision_id,
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("PreconditionFailedException",):
                current = self.get_alias_state(function_name, alias_name)
                raise AliasRevisionStale(expected_revision_id, current.alias_revision_id) from exc
            raise
        return self.get_alias_state(function_name, alias_name)

    def invoke(self, function_name: str, alias_name: str, payload: dict[str, Any]) -> InvokeResult:
        import json

        response = self._lambda.invoke(
            FunctionName=function_name,
            Qualifier=alias_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8"),
        )
        raw_body = response["Payload"].read()
        body: dict[str, Any] | None
        try:
            body = json.loads(raw_body) if raw_body else None
        except json.JSONDecodeError:
            body = None
        function_error = response.get("FunctionError")
        return InvokeResult(
            request_id=payload.get("request_id", ""),
            executed_version=response.get("ExecutedVersion", ""),
            function_error=function_error,
            status_code=response["StatusCode"],
            payload=None if function_error else (body.get("body") if isinstance(body, dict) else body),
            error_message=(body.get("errorMessage") if isinstance(body, dict) else None) if function_error else None,
            aws_request_id=response.get("ResponseMetadata", {}).get("RequestId", ""),
        )

    def get_error_log_events(self, log_group_name: str, window_minutes: int, limit: int) -> list[LogEvent]:
        from datetime import timedelta

        start_ms = int((datetime.now(UTC) - timedelta(minutes=window_minutes)).timestamp() * 1000)
        try:
            response = self._logs.filter_log_events(
                logGroupName=log_group_name,
                startTime=start_ms,
                filterPattern='"Task timed out" OR "ERROR" OR "Unhandled" OR "AccessDenied"',
                limit=min(limit, 1000),
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                return []
            raise
        events: list[LogEvent] = []
        for i, event in enumerate(response.get("events", [])[:limit]):
            events.append(
                LogEvent(
                    evidence_source_ref=f"{log_group_name}#{event.get('eventId', i)}",
                    timestamp=datetime.fromtimestamp(event["timestamp"] / 1000, tz=UTC),
                    request_id=_extract_request_id(event["message"]),
                    error_type=_classify_error(event["message"]),
                    message=event["message"][:2000],
                )
            )
        return events

    def get_result(self, table_ref: str, request_id: str) -> ResultRecord | None:
        table = self._dynamodb.Table(table_ref)
        item = table.get_item(Key={"request_id": request_id}).get("Item")
        if item is None:
            return None
        return ResultRecord(
            request_id=item["request_id"],
            payload_sha256=item["payload_sha256"],
            processed_at=item["processed_at"],
            document_type=item["document_type"],
            page_count=int(item["page_count"]),
        )


def _config_fingerprint(env: dict[str, str]) -> str:
    import hashlib
    import json

    allowlisted = {k: env[k] for k in _ALLOWLISTED_ENV_FIELDS if k in env}
    encoded = json.dumps(allowlisted, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _extract_request_id(message: str) -> str | None:
    import re

    match = re.search(r'"?request_id"?[=:]\s*"?([A-Za-z0-9_\-]+)"?', message)
    return match.group(1) if match else None


def _classify_error(message: str) -> str:
    if "AccessDenied" in message:
        return "AccessDeniedException"
    if "Task timed out" in message:
        return "Timeout"
    if "Unhandled" in message:
        return "Unhandled"
    return "Error"
