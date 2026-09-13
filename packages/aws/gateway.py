"""The ``AwsGateway`` protocol and its plain-data result types.

One interface, two implementations (``fixture_gateway.FixtureAwsGateway``,
``live_gateway.LiveAwsGateway``). Every caller -- the MCP evidence tools,
the worker's dispatcher, the executor -- depends only on this protocol, so
"fixture vs live" is a single dependency-injection decision made once at
process startup (``services/api/config.py``), never branched on ad hoc.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


class AliasRevisionStale(Exception):
    """The alias moved since the caller last read its revision id."""

    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"alias revision changed: expected {expected!r}, now {actual!r}")


class ResultsTableUnavailable(Exception):
    def __init__(self, table_ref: str, cause: str) -> None:
        self.table_ref = table_ref
        self.cause = cause
        super().__init__(f"results table {table_ref!r} unavailable: {cause}")


@dataclass(frozen=True)
class AliasState:
    function_name: str
    alias_name: str
    current_version: str
    alias_revision_id: str
    weighted_routing: bool
    config_fingerprint: str
    results_table_ref: str
    updated_at: datetime


@dataclass(frozen=True)
class InvokeResult:
    request_id: str
    executed_version: str
    function_error: str | None  # e.g. "Unhandled" -- None means no error
    status_code: int
    payload: dict[str, Any] | None
    error_message: str | None = None
    aws_request_id: str = ""


@dataclass(frozen=True)
class LogEvent:
    evidence_source_ref: str
    timestamp: datetime
    request_id: str | None
    error_type: str
    message: str


@dataclass(frozen=True)
class ResultRecord:
    request_id: str
    payload_sha256: str
    processed_at: str
    document_type: str
    page_count: int


@dataclass(frozen=True)
class PutOutcome:
    inserted: bool
    already_completed: bool
    conflict: bool


class AwsGateway(Protocol):
    """Read + (for the executor only) mutate the monitored application."""

    mode: str  # "fixture" | "aws_live"

    def get_alias_state(self, function_name: str, alias_name: str) -> AliasState: ...

    def update_alias(
        self, function_name: str, alias_name: str, expected_revision_id: str, target_version: str
    ) -> AliasState:
        """Raises ``AliasRevisionStale`` if ``expected_revision_id`` doesn't match live state."""

    def invoke(self, function_name: str, alias_name: str, payload: dict[str, Any]) -> InvokeResult: ...

    def get_error_log_events(self, log_group_name: str, window_minutes: int, limit: int) -> list[LogEvent]: ...

    def get_result(self, table_ref: str, request_id: str) -> ResultRecord | None: ...
