"""Simulated AWS: no network calls, same business logic as production.

Backed by its own SQLite file (default ``data/fixture_aws.db``) so the API
process, the worker process, and the owner-only fault-injection CLI all see
one consistent simulated Lambda alias / DynamoDB results tables / CloudWatch
log group, exactly as they would share real AWS state.

This is explicitly labeled fixture data everywhere it surfaces (``mode:
"fixture"`` in ``/v1/capabilities``, a banner in the UI) -- see build brief:
"An offline fixture mode, explicitly labeled, for reproducible tests and UI
development" and "Do not claim requests recovered unless their results were
verified" (fixture verification runs the identical checks live mode runs,
just against simulated state).
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from demo.workload import processor
from packages.aws.gateway import (
    AliasRevisionStale,
    AliasState,
    InvokeResult,
    LogEvent,
    ResultRecord,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alias_state (
    function_name TEXT NOT NULL, alias_name TEXT NOT NULL,
    current_version TEXT NOT NULL, alias_revision_id TEXT NOT NULL,
    weighted_routing INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
    PRIMARY KEY (function_name, alias_name)
);

CREATE TABLE IF NOT EXISTS deployment_version (
    function_name TEXT NOT NULL, version TEXT NOT NULL,
    results_table_ref TEXT NOT NULL, config_fingerprint TEXT NOT NULL,
    can_access INTEGER NOT NULL,
    PRIMARY KEY (function_name, version)
);

CREATE TABLE IF NOT EXISTS results_table (
    table_ref TEXT NOT NULL, request_id TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
    processed_at TEXT NOT NULL, document_type TEXT NOT NULL, page_count INTEGER NOT NULL,
    PRIMARY KEY (table_ref, request_id)
);

CREATE TABLE IF NOT EXISTS log_event (
    function_name TEXT NOT NULL, ts TEXT NOT NULL, request_id TEXT, error_type TEXT NOT NULL,
    message TEXT NOT NULL, seq INTEGER
);
"""


def _revision_id(version: str, weighted: bool, seed: str) -> str:
    raw = f"{version}:{weighted}:{seed}".encode()
    return hashlib.sha1(raw).hexdigest()[:14]


class FixtureAwsGateway:
    mode = "fixture"

    def __init__(self, path: str | Path = "data/fixture_aws.db") -> None:
        self._path = str(path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, timeout=30, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------------
    # Setup / fault-injection only (not part of AwsGateway protocol) --
    # called from scripts/seed_fixture.py and scripts/inject_fault.py.
    # ------------------------------------------------------------------
    def register_version(
        self,
        function_name: str,
        version: str,
        results_table_ref: str,
        config_fingerprint: str,
        can_access: bool,
    ) -> None:
        self._conn().execute(
            "INSERT INTO deployment_version(function_name, version, results_table_ref, "
            "config_fingerprint, can_access) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(function_name, version) DO UPDATE SET "
            "results_table_ref=excluded.results_table_ref, "
            "config_fingerprint=excluded.config_fingerprint, can_access=excluded.can_access",
            (function_name, version, results_table_ref, config_fingerprint, int(can_access)),
        )

    def force_set_alias(self, function_name: str, alias_name: str, version: str, weighted: bool = False) -> AliasState:
        """Owner-only: point the alias at ``version`` without a revision check.

        Used for initial seeding (pointing at G) and by the fault-injection
        CLI (pointing at B) -- both are setup actions, not the executor's
        approved-rollback path, which must always go through ``update_alias``.
        """
        now = datetime.now(UTC)
        revision_id = _revision_id(version, weighted, now.isoformat())
        self._conn().execute(
            "INSERT INTO alias_state(function_name, alias_name, current_version, alias_revision_id, "
            "weighted_routing, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(function_name, alias_name) DO UPDATE SET current_version=excluded.current_version, "
            "alias_revision_id=excluded.alias_revision_id, weighted_routing=excluded.weighted_routing, "
            "updated_at=excluded.updated_at",
            (function_name, alias_name, version, revision_id, int(weighted), now.isoformat()),
        )
        return self.get_alias_state(function_name, alias_name)

    def reset(self) -> None:
        self._conn().executescript(
            "DELETE FROM alias_state; DELETE FROM deployment_version; DELETE FROM results_table; DELETE FROM log_event;"
        )

    # ------------------------------------------------------------------
    # AwsGateway protocol
    # ------------------------------------------------------------------
    def get_alias_state(self, function_name: str, alias_name: str) -> AliasState:
        row = (
            self._conn()
            .execute(
                "SELECT * FROM alias_state WHERE function_name=? AND alias_name=?",
                (function_name, alias_name),
            )
            .fetchone()
        )
        if row is None:
            raise LookupError(f"no alias state for {function_name}:{alias_name}; run seed_fixture.py")
        dep = (
            self._conn()
            .execute(
                "SELECT * FROM deployment_version WHERE function_name=? AND version=?",
                (function_name, row["current_version"]),
            )
            .fetchone()
        )
        return AliasState(
            function_name=function_name,
            alias_name=alias_name,
            current_version=row["current_version"],
            alias_revision_id=row["alias_revision_id"],
            weighted_routing=bool(row["weighted_routing"]),
            config_fingerprint=dep["config_fingerprint"] if dep else "",
            results_table_ref=dep["results_table_ref"] if dep else "",
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def update_alias(
        self, function_name: str, alias_name: str, expected_revision_id: str, target_version: str
    ) -> AliasState:
        current = self.get_alias_state(function_name, alias_name)
        if current.alias_revision_id != expected_revision_id:
            raise AliasRevisionStale(expected_revision_id, current.alias_revision_id)
        return self.force_set_alias(function_name, alias_name, target_version, weighted=False)

    def invoke(self, function_name: str, alias_name: str, payload: dict[str, Any]) -> InvokeResult:
        state = self.get_alias_state(function_name, alias_name)
        dep = (
            self._conn()
            .execute(
                "SELECT * FROM deployment_version WHERE function_name=? AND version=?",
                (function_name, state.current_version),
            )
            .fetchone()
        )
        table_ref = dep["results_table_ref"]
        can_access = bool(dep["can_access"])
        request_id = payload["request_id"]

        adapter = _FixtureResultsTable(self, table_ref, can_access)
        try:
            result = processor.handle(payload, results_table=adapter, results_table_ref=table_ref)
            return InvokeResult(
                request_id=request_id,
                executed_version=state.current_version,
                function_error=None,
                status_code=200,
                payload=result,
            )
        except processor.ResultsTableUnavailable as exc:
            self._log(
                function_name,
                request_id=request_id,
                error_type="ResultsTableUnavailable",
                message=str(exc),
            )
            return InvokeResult(
                request_id=request_id,
                executed_version=state.current_version,
                function_error="Unhandled",
                status_code=200,  # Lambda invoke() itself succeeds; FunctionError signals the failure
                payload=None,
                error_message=str(exc),
            )

    def get_error_log_events(self, log_group_name: str, window_minutes: int, limit: int) -> list[LogEvent]:
        function_name = log_group_name.rsplit("/", 1)[-1]
        cutoff = datetime.now(UTC) - timedelta(minutes=window_minutes)
        rows = (
            self._conn()
            .execute(
                "SELECT * FROM log_event WHERE function_name=? AND ts>=? ORDER BY seq DESC LIMIT ?",
                (function_name, cutoff.isoformat(), limit),
            )
            .fetchall()
        )
        return [
            LogEvent(
                evidence_source_ref=f"{log_group_name}#{r['seq']}",
                timestamp=datetime.fromisoformat(r["ts"]),
                request_id=r["request_id"],
                error_type=r["error_type"],
                message=r["message"],
            )
            for r in rows
        ]

    def get_result(self, table_ref: str, request_id: str) -> ResultRecord | None:
        row = (
            self._conn()
            .execute("SELECT * FROM results_table WHERE table_ref=? AND request_id=?", (table_ref, request_id))
            .fetchone()
        )
        if row is None:
            return None
        return ResultRecord(
            request_id=row["request_id"],
            payload_sha256=row["payload_sha256"],
            processed_at=row["processed_at"],
            document_type=row["document_type"],
            page_count=row["page_count"],
        )

    def _log(self, function_name: str, *, request_id: str | None, error_type: str, message: str) -> None:
        seq = (
            self._conn()
            .execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM log_event WHERE function_name=?", (function_name,))
            .fetchone()["n"]
        )
        self._conn().execute(
            "INSERT INTO log_event(function_name, ts, request_id, error_type, message, seq) VALUES (?, ?, ?, ?, ?, ?)",
            (function_name, datetime.now(UTC).isoformat(), request_id, error_type, message, seq),
        )


class _FixtureResultsTable:
    """Adapter satisfying ``demo.workload.processor.ResultsTable`` over SQLite."""

    def __init__(self, gw: FixtureAwsGateway, table_ref: str, can_access: bool) -> None:
        self._gw = gw
        self._table_ref = table_ref
        self._can_access = can_access

    def conditional_put(self, request_id: str, payload_sha256: str, item: dict[str, Any]) -> processor.PutOutcome:
        if not self._can_access:
            raise processor.ResultsTableUnavailable(
                self._table_ref,
                "AccessDeniedException: processor role is not authorized to perform dynamodb:PutItem "
                f"on resource {self._table_ref}",
            )
        conn = self._gw._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT payload_sha256 FROM results_table WHERE table_ref=? AND request_id=?",
                (self._table_ref, request_id),
            ).fetchone()
            if existing is not None:
                conn.execute("COMMIT")
                same = existing["payload_sha256"] == payload_sha256
                return processor.PutOutcome(
                    inserted=False,
                    already_completed=same,
                    conflict=not same,
                    result_id=request_id,
                    payload_sha256=payload_sha256,
                )
            conn.execute(
                "INSERT INTO results_table(table_ref, request_id, payload_sha256, processed_at, "
                "document_type, page_count) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self._table_ref,
                    request_id,
                    payload_sha256,
                    item["processed_at"],
                    item["document_type"],
                    item["page_count"],
                ),
            )
            conn.execute("COMMIT")
            return processor.PutOutcome(
                inserted=True,
                already_completed=False,
                conflict=False,
                result_id=request_id,
                payload_sha256=payload_sha256,
            )
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def get(self, request_id: str) -> dict[str, Any] | None:
        row = (
            self._gw._conn()
            .execute(
                "SELECT * FROM results_table WHERE table_ref=? AND request_id=?",
                (self._table_ref, request_id),
            )
            .fetchone()
        )
        return dict(row) if row else None
