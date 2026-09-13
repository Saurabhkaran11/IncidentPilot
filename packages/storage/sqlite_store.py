"""SQLite implementation of ``ControlPlaneStore``.

Backs fixture mode and the P0 local-live demo (build brief section 7:
"FastAPI serves the built frontend and uses SQLite for durable incident/job
state in fixture mode"). One file, WAL mode for the API process and the
local worker to share safely, ``BEGIN IMMEDIATE`` transactions for every
conditional write so concurrent requests can't interleave a read-modify-write.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

from packages.domain.config import AppConfig, DeploymentManifestEntry
from packages.domain.models import (
    Approval,
    DemoRequest,
    DemoRun,
    Diagnosis,
    Evidence,
    ExecutionRecord,
    Incident,
    Operation,
    OutboxEvent,
    Receipt,
)
from packages.domain.plan import RecoveryPlan
from packages.storage.interface import (
    ConcurrencyConflict,
    ControlPlaneStore,
    EventPage,
    IdempotencyRecord,
    IncidentPage,
    OperatorTokenRecord,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_config (app_id TEXT PRIMARY KEY, json TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS deployment_version (
    app_id TEXT NOT NULL, version TEXT NOT NULL, json TEXT NOT NULL,
    PRIMARY KEY (app_id, version)
);

CREATE TABLE IF NOT EXISTS demo_run (demo_run_id TEXT PRIMARY KEY, json TEXT NOT NULL, lock_operation_id TEXT);

CREATE TABLE IF NOT EXISTS demo_request (
    request_id TEXT PRIMARY KEY, demo_run_id TEXT NOT NULL, json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_demo_request_run ON demo_request(demo_run_id);

CREATE TABLE IF NOT EXISTS incident (
    incident_id TEXT PRIMARY KEY, app_id TEXT NOT NULL, demo_run_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL,
    updated_at TEXT NOT NULL, json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incident_active ON incident(app_id, demo_run_id, fingerprint, state);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, run_id TEXT NOT NULL, json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_run ON evidence(run_id);

CREATE TABLE IF NOT EXISTS diagnosis (
    diagnosis_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, created_at TEXT NOT NULL, json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_diagnosis_incident ON diagnosis(incident_id, created_at);

CREATE TABLE IF NOT EXISTS plan (
    plan_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, digest TEXT NOT NULL,
    canonical_bytes BLOB NOT NULL, json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approval (
    approval_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL UNIQUE, json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operation (
    operation_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL,
    kind TEXT NOT NULL, status TEXT NOT NULL, json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operation_incident ON operation(incident_id);
CREATE INDEX IF NOT EXISTS idx_operation_kind_status ON operation(kind, status);

CREATE TABLE IF NOT EXISTS execution_record (
    operation_id TEXT PRIMARY KEY, lease_owner TEXT, lease_version INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TEXT, json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receipt (
    incident_id TEXT NOT NULL, receipt_id TEXT NOT NULL, created_at TEXT NOT NULL, json TEXT NOT NULL,
    PRIMARY KEY (incident_id, receipt_id)
);

CREATE TABLE IF NOT EXISTS event (
    incident_id TEXT NOT NULL, sequence INTEGER NOT NULL, json TEXT NOT NULL,
    PRIMARY KEY (incident_id, sequence)
);

CREATE TABLE IF NOT EXISTS idempotency (
    scope_key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL, status_code INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS source_event (
    source TEXT NOT NULL, source_event_id TEXT NOT NULL, incident_id TEXT NOT NULL,
    PRIMARY KEY (source, source_event_id)
);

CREATE TABLE IF NOT EXISTS operator_token (
    token_hash TEXT PRIMARY KEY, actor_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
    aws_identity_arn TEXT NOT NULL, issued_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    boot_session_id TEXT NOT NULL
);
"""


class SqliteControlPlaneStore(ControlPlaneStore):
    def __init__(self, path: str | Path) -> None:
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
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _tx(self):
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        return conn

    # -- app config --
    def get_app_config(self, app_id: str) -> AppConfig | None:
        row = self._conn().execute("SELECT json FROM app_config WHERE app_id=?", (app_id,)).fetchone()
        return AppConfig.model_validate_json(row["json"]) if row else None

    def put_app_config(self, config: AppConfig) -> None:
        self._conn().execute(
            "INSERT INTO app_config(app_id, json) VALUES (?, ?) "
            "ON CONFLICT(app_id) DO UPDATE SET json=excluded.json",
            (config.app_id, config.model_dump_json()),
        )

    def record_deployment_version(self, entry: DeploymentManifestEntry) -> None:
        self._conn().execute(
            "INSERT INTO deployment_version(app_id, version, json) VALUES (?, ?, ?) "
            "ON CONFLICT(app_id, version) DO UPDATE SET json=excluded.json",
            (entry.app_id, entry.version, entry.model_dump_json()),
        )

    def get_deployment_version(self, app_id: str, version: str) -> DeploymentManifestEntry | None:
        row = self._conn().execute(
            "SELECT json FROM deployment_version WHERE app_id=? AND version=?", (app_id, version)
        ).fetchone()
        return DeploymentManifestEntry.model_validate_json(row["json"]) if row else None

    def get_known_good_deployment(self, app_id: str) -> DeploymentManifestEntry | None:
        rows = self._conn().execute(
            "SELECT json FROM deployment_version WHERE app_id=?", (app_id,)
        ).fetchall()
        for row in rows:
            entry = DeploymentManifestEntry.model_validate_json(row["json"])
            if entry.is_known_good:
                return entry
        return None

    # -- demo runs / requests --
    def create_demo_run(self, run: DemoRun) -> None:
        self._conn().execute(
            "INSERT INTO demo_run(demo_run_id, json, lock_operation_id) VALUES (?, ?, NULL)",
            (run.demo_run_id, run.model_dump_json()),
        )

    def get_demo_run(self, demo_run_id: str) -> DemoRun | None:
        row = self._conn().execute(
            "SELECT json FROM demo_run WHERE demo_run_id=?", (demo_run_id,)
        ).fetchone()
        return DemoRun.model_validate_json(row["json"]) if row else None

    def try_lock_run(self, demo_run_id: str, operation_id: str) -> bool:
        conn = self._tx()
        try:
            row = conn.execute(
                "SELECT lock_operation_id FROM demo_run WHERE demo_run_id=?", (demo_run_id,)
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return False
            if row["lock_operation_id"] not in (None, operation_id):
                conn.execute("COMMIT")
                return False
            conn.execute(
                "UPDATE demo_run SET lock_operation_id=? WHERE demo_run_id=?",
                (operation_id, demo_run_id),
            )
            conn.execute("COMMIT")
            return True
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def release_run_lock(self, demo_run_id: str, operation_id: str) -> None:
        self._conn().execute(
            "UPDATE demo_run SET lock_operation_id=NULL WHERE demo_run_id=? AND lock_operation_id=?",
            (demo_run_id, operation_id),
        )

    def create_demo_request(self, request: DemoRequest) -> None:
        self._conn().execute(
            "INSERT INTO demo_request(request_id, demo_run_id, json) VALUES (?, ?, ?)",
            (request.request_id, request.demo_run_id, request.model_dump_json()),
        )

    def get_demo_request(self, request_id: str) -> DemoRequest | None:
        row = self._conn().execute(
            "SELECT json FROM demo_request WHERE request_id=?", (request_id,)
        ).fetchone()
        return DemoRequest.model_validate_json(row["json"]) if row else None

    def list_demo_requests(self, demo_run_id: str) -> list[DemoRequest]:
        rows = self._conn().execute(
            "SELECT json FROM demo_request WHERE demo_run_id=? ORDER BY rowid", (demo_run_id,)
        ).fetchall()
        return [DemoRequest.model_validate_json(r["json"]) for r in rows]

    def update_demo_request(
        self, request_id: str, mutator: Callable[[DemoRequest], DemoRequest]
    ) -> DemoRequest:
        conn = self._tx()
        try:
            row = conn.execute(
                "SELECT json FROM demo_request WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise KeyError(request_id)
            current = DemoRequest.model_validate_json(row["json"])
            updated = mutator(current)
            conn.execute(
                "UPDATE demo_request SET json=? WHERE request_id=?",
                (updated.model_dump_json(), request_id),
            )
            conn.execute("COMMIT")
            return updated
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    # -- incidents --
    def create_incident(self, incident: Incident) -> None:
        self._conn().execute(
            "INSERT INTO incident(incident_id, app_id, demo_run_id, fingerprint, state, version, "
            "updated_at, json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                incident.incident_id,
                incident.app_id,
                incident.demo_run_id,
                incident.fingerprint,
                incident.state.value,
                incident.version,
                incident.updated_at.isoformat(),
                incident.model_dump_json(),
            ),
        )

    def get_incident(self, incident_id: str) -> Incident | None:
        row = self._conn().execute(
            "SELECT json FROM incident WHERE incident_id=?", (incident_id,)
        ).fetchone()
        return Incident.model_validate_json(row["json"]) if row else None

    def find_active_incident(self, app_id: str, demo_run_id: str, fingerprint: str) -> Incident | None:
        from packages.domain.enums import TERMINAL_INCIDENT_STATES

        rows = self._conn().execute(
            "SELECT json FROM incident WHERE app_id=? AND demo_run_id=? AND fingerprint=?",
            (app_id, demo_run_id, fingerprint),
        ).fetchall()
        terminal = {s.value for s in TERMINAL_INCIDENT_STATES}
        candidates = [Incident.model_validate_json(r["json"]) for r in rows]
        active = [c for c in candidates if c.state.value not in terminal]
        active.sort(key=lambda c: c.created_at)
        return active[-1] if active else None

    def update_incident(
        self, incident_id: str, expected_version: int, mutator: Callable[[Incident], Incident]
    ) -> Incident:
        conn = self._tx()
        try:
            row = conn.execute(
                "SELECT json, version FROM incident WHERE incident_id=?", (incident_id,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise KeyError(incident_id)
            if row["version"] != expected_version:
                conn.execute("ROLLBACK")
                raise ConcurrencyConflict(
                    f"incident {incident_id} is at version {row['version']}, expected {expected_version}"
                )
            current = Incident.model_validate_json(row["json"])
            updated = mutator(current)
            if updated.version != expected_version + 1:
                conn.execute("ROLLBACK")
                raise ValueError("mutator must increment version by exactly 1")
            conn.execute(
                "UPDATE incident SET state=?, version=?, updated_at=?, json=? WHERE incident_id=?",
                (
                    updated.state.value,
                    updated.version,
                    updated.updated_at.isoformat(),
                    updated.model_dump_json(),
                    incident_id,
                ),
            )
            conn.execute("COMMIT")
            return updated
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def list_incidents(self, cursor: str | None, limit: int) -> IncidentPage:
        offset = int(cursor) if cursor else 0
        rows = self._conn().execute(
            "SELECT json FROM incident ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (limit + 1, offset),
        ).fetchall()
        items = [Incident.model_validate_json(r["json"]) for r in rows[:limit]]
        next_cursor = str(offset + limit) if len(rows) > limit else None
        return IncidentPage(items=items, next_cursor=next_cursor)

    # -- evidence / diagnosis --
    def put_evidence(self, evidence: Evidence) -> None:
        self._conn().execute(
            "INSERT INTO evidence(evidence_id, incident_id, run_id, json) VALUES (?, ?, ?, ?)",
            (evidence.evidence_id, evidence.incident_id, evidence.run_id, evidence.model_dump_json()),
        )

    def get_evidence(self, incident_id: str, evidence_id: str) -> Evidence | None:
        row = self._conn().execute(
            "SELECT json FROM evidence WHERE incident_id=? AND evidence_id=?",
            (incident_id, evidence_id),
        ).fetchone()
        return Evidence.model_validate_json(row["json"]) if row else None

    def list_evidence_for_run(self, run_id: str) -> list[Evidence]:
        rows = self._conn().execute(
            "SELECT json FROM evidence WHERE run_id=? ORDER BY rowid", (run_id,)
        ).fetchall()
        return [Evidence.model_validate_json(r["json"]) for r in rows]

    def put_diagnosis(self, diagnosis: Diagnosis) -> None:
        self._conn().execute(
            "INSERT INTO diagnosis(diagnosis_id, incident_id, created_at, json) VALUES (?, ?, ?, ?)",
            (
                diagnosis.diagnosis_id,
                diagnosis.incident_id,
                diagnosis.created_at.isoformat(),
                diagnosis.model_dump_json(),
            ),
        )

    def get_latest_diagnosis(self, incident_id: str) -> Diagnosis | None:
        row = self._conn().execute(
            "SELECT json FROM diagnosis WHERE incident_id=? ORDER BY created_at DESC LIMIT 1",
            (incident_id,),
        ).fetchone()
        return Diagnosis.model_validate_json(row["json"]) if row else None

    # -- plans / approvals --
    def put_plan(self, plan: RecoveryPlan, canonical_bytes: bytes, digest: str) -> None:
        self._conn().execute(
            "INSERT INTO plan(plan_id, incident_id, digest, canonical_bytes, json) VALUES (?, ?, ?, ?, ?)",
            (plan.plan_id, plan.incident_id, digest, canonical_bytes, plan.model_dump_json()),
        )

    def get_plan(self, plan_id: str) -> RecoveryPlan | None:
        row = self._conn().execute("SELECT json FROM plan WHERE plan_id=?", (plan_id,)).fetchone()
        return RecoveryPlan.model_validate_json(row["json"]) if row else None

    def get_plan_digest(self, plan_id: str) -> str | None:
        row = self._conn().execute("SELECT digest FROM plan WHERE plan_id=?", (plan_id,)).fetchone()
        return row["digest"] if row else None

    def put_approval(self, approval: Approval) -> None:
        try:
            self._conn().execute(
                "INSERT INTO approval(approval_id, plan_id, json) VALUES (?, ?, ?)",
                (approval.approval_id, approval.plan_id, approval.model_dump_json()),
            )
        except sqlite3.IntegrityError as exc:
            raise ConcurrencyConflict(f"plan {approval.plan_id} already has a decision") from exc

    def get_approval(self, approval_id: str) -> Approval | None:
        row = self._conn().execute(
            "SELECT json FROM approval WHERE approval_id=?", (approval_id,)
        ).fetchone()
        return Approval.model_validate_json(row["json"]) if row else None

    def get_approval_for_plan(self, plan_id: str) -> Approval | None:
        row = self._conn().execute("SELECT json FROM approval WHERE plan_id=?", (plan_id,)).fetchone()
        return Approval.model_validate_json(row["json"]) if row else None

    # -- operations --
    def create_operation(self, operation: Operation) -> None:
        self._conn().execute(
            "INSERT INTO operation(operation_id, incident_id, kind, status, json) VALUES (?, ?, ?, ?, ?)",
            (
                operation.operation_id,
                operation.incident_id,
                operation.kind.value,
                operation.status.value,
                operation.model_dump_json(),
            ),
        )

    def get_operation(self, operation_id: str) -> Operation | None:
        row = self._conn().execute(
            "SELECT json FROM operation WHERE operation_id=?", (operation_id,)
        ).fetchone()
        return Operation.model_validate_json(row["json"]) if row else None

    def update_operation(self, operation_id: str, mutator: Callable[[Operation], Operation]) -> Operation:
        conn = self._tx()
        try:
            row = conn.execute(
                "SELECT json FROM operation WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise KeyError(operation_id)
            updated = mutator(Operation.model_validate_json(row["json"]))
            conn.execute(
                "UPDATE operation SET status=?, json=? WHERE operation_id=?",
                (updated.status.value, updated.model_dump_json(), operation_id),
            )
            conn.execute("COMMIT")
            return updated
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def list_operations_by_status(self, kind: str, status: str, limit: int) -> list[Operation]:
        rows = self._conn().execute(
            "SELECT json FROM operation WHERE kind=? AND status=? ORDER BY rowid LIMIT ?",
            (kind, status, limit),
        ).fetchall()
        return [Operation.model_validate_json(r["json"]) for r in rows]

    def put_execution_record(self, record: ExecutionRecord) -> None:
        self._conn().execute(
            "INSERT INTO execution_record(operation_id, lease_owner, lease_version, "
            "lease_expires_at, json) VALUES (?, ?, ?, ?, ?)",
            (
                record.operation_id,
                record.lease_owner,
                record.lease_version,
                record.lease_expires_at.isoformat() if record.lease_expires_at else None,
                record.model_dump_json(),
            ),
        )

    def get_execution_record(self, operation_id: str) -> ExecutionRecord | None:
        row = self._conn().execute(
            "SELECT json FROM execution_record WHERE operation_id=?", (operation_id,)
        ).fetchone()
        return ExecutionRecord.model_validate_json(row["json"]) if row else None

    def update_execution_record(
        self, operation_id: str, mutator: Callable[[ExecutionRecord], ExecutionRecord]
    ) -> ExecutionRecord:
        conn = self._tx()
        try:
            row = conn.execute(
                "SELECT json FROM execution_record WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise KeyError(operation_id)
            updated = mutator(ExecutionRecord.model_validate_json(row["json"]))
            conn.execute(
                "UPDATE execution_record SET lease_owner=?, lease_version=?, "
                "lease_expires_at=?, json=? WHERE operation_id=?",
                (
                    updated.lease_owner,
                    updated.lease_version,
                    updated.lease_expires_at.isoformat() if updated.lease_expires_at else None,
                    updated.model_dump_json(),
                    operation_id,
                ),
            )
            conn.execute("COMMIT")
            return updated
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def claim_execution_lease(self, operation_id: str, owner: str, lease_seconds: int) -> bool:
        from datetime import UTC, datetime, timedelta

        conn = self._tx()
        try:
            row = conn.execute(
                "SELECT lease_owner, lease_expires_at, lease_version, json "
                "FROM execution_record WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return False
            now = datetime.now(UTC)
            expired = row["lease_expires_at"] is None or datetime.fromisoformat(
                row["lease_expires_at"]
            ) < now
            if row["lease_owner"] not in (None, owner) and not expired:
                conn.execute("ROLLBACK")
                return False
            new_expiry = now + timedelta(seconds=lease_seconds)
            record = ExecutionRecord.model_validate_json(row["json"])
            record = record.model_copy(
                update={
                    "lease_owner": owner,
                    "lease_version": row["lease_version"] + 1,
                    "lease_expires_at": new_expiry,
                }
            )
            conn.execute(
                "UPDATE execution_record SET lease_owner=?, lease_version=?, "
                "lease_expires_at=?, json=? WHERE operation_id=?",
                (owner, record.lease_version, new_expiry.isoformat(), record.model_dump_json(), operation_id),
            )
            conn.execute("COMMIT")
            return True
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    # -- receipts --
    def put_receipt(self, receipt: Receipt) -> None:
        self._conn().execute(
            "INSERT INTO receipt(incident_id, receipt_id, created_at, json) VALUES (?, ?, ?, ?)",
            (
                receipt.incident_id,
                receipt.receipt_id,
                receipt.created_at.isoformat(),
                receipt.model_dump_json(),
            ),
        )

    def get_latest_receipt(self, incident_id: str) -> Receipt | None:
        row = self._conn().execute(
            "SELECT json FROM receipt WHERE incident_id=? ORDER BY created_at DESC LIMIT 1",
            (incident_id,),
        ).fetchone()
        return Receipt.model_validate_json(row["json"]) if row else None

    # -- events --
    def append_event(self, incident_id: str, type_: str, data: dict) -> OutboxEvent:
        from datetime import UTC, datetime

        from packages.domain.ids import new_id

        conn = self._tx()
        try:
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS max_seq FROM event WHERE incident_id=?",
                (incident_id,),
            ).fetchone()
            next_seq = row["max_seq"] + 1
            event = OutboxEvent(
                event_id=new_id("event"),
                aggregate_id=incident_id,
                sequence=next_seq,
                type=type_,
                occurred_at=datetime.now(UTC),
                data=data,
                delivered=True,
            )
            conn.execute(
                "INSERT INTO event(incident_id, sequence, json) VALUES (?, ?, ?)",
                (incident_id, next_seq, event.model_dump_json()),
            )
            conn.execute("COMMIT")
            return event
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def list_events(self, incident_id: str, after_sequence: int, limit: int) -> EventPage:
        rows = self._conn().execute(
            "SELECT json FROM event WHERE incident_id=? AND sequence>? ORDER BY sequence LIMIT ?",
            (incident_id, after_sequence, limit + 1),
        ).fetchall()
        items = [OutboxEvent.model_validate_json(r["json"]) for r in rows[:limit]]
        has_more = len(rows) > limit
        last_sequence = items[-1].sequence if items else after_sequence
        return EventPage(items=items, last_sequence=last_sequence, has_more=has_more)

    # -- idempotency --
    def get_idempotency_record(self, scope_key: str) -> IdempotencyRecord | None:
        row = self._conn().execute(
            "SELECT * FROM idempotency WHERE scope_key=?", (scope_key,)
        ).fetchone()
        if row is None:
            return None
        return IdempotencyRecord(
            scope_key=row["scope_key"],
            request_hash=row["request_hash"],
            resource_id=row["resource_id"],
            response_json=row["response_json"],
            status_code=row["status_code"],
        )

    def put_idempotency_record(self, record: IdempotencyRecord) -> None:
        try:
            self._conn().execute(
                "INSERT INTO idempotency(scope_key, request_hash, resource_id, response_json, "
                "status_code) VALUES (?, ?, ?, ?, ?)",
                (
                    record.scope_key,
                    record.request_hash,
                    record.resource_id,
                    record.response_json,
                    record.status_code,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConcurrencyConflict(f"idempotency key {record.scope_key} already used") from exc

    # -- source events --
    def seen_source_event(self, source: str, source_event_id: str) -> str | None:
        row = self._conn().execute(
            "SELECT incident_id FROM source_event WHERE source=? AND source_event_id=?",
            (source, source_event_id),
        ).fetchone()
        return row["incident_id"] if row else None

    def record_source_event(self, source: str, source_event_id: str, incident_id: str) -> None:
        self._conn().execute(
            "INSERT OR IGNORE INTO source_event(source, source_event_id, incident_id) VALUES (?, ?, ?)",
            (source, source_event_id, incident_id),
        )

    # -- operator tokens --
    def put_operator_token(self, record: OperatorTokenRecord) -> None:
        self._conn().execute(
            "INSERT INTO operator_token(token_hash, actor_id, workspace_id, aws_identity_arn, "
            "issued_at, expires_at, boot_session_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                record.token_hash,
                record.actor_id,
                record.workspace_id,
                record.aws_identity_arn,
                record.issued_at,
                record.expires_at,
                record.boot_session_id,
            ),
        )

    def get_operator_token(self, token_hash: str) -> OperatorTokenRecord | None:
        row = self._conn().execute(
            "SELECT * FROM operator_token WHERE token_hash=?", (token_hash,)
        ).fetchone()
        if row is None:
            return None
        return OperatorTokenRecord(**{k: row[k] for k in row.keys()})

    def revoke_all_operator_tokens(self) -> None:
        self._conn().execute("DELETE FROM operator_token")
