"""One isolated, seeded fixture world per test.

``scripts/run_fixture_demo.py`` walks the same steps procedurally; the
``World`` helper below is that walkthrough factored into reusable pieces so
each test can stop at, or perturb, exactly one step. Everything lives under
``tmp_path`` and talks only to ``FixtureAwsGateway`` + SQLite: no network,
no AWS credentials, no Bedrock.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

import scripts.seed_fixture as seed_fixture
from demo.workload.processor import canonicalize_input, payload_sha256
from mcp_server import tools as evidence_tools
from mcp_server.context import RunContext
from packages.aws.fixture_gateway import FixtureAwsGateway
from packages.domain.config import AppConfig, DeploymentManifestEntry
from packages.domain.enums import (
    Decision,
    IncidentState,
    Mode,
    OperationKind,
    OperationStatus,
    RecommendedAction,
    RequestStatus,
)
from packages.domain.ids import new_id
from packages.domain.models import (
    Approval,
    DemoDocument,
    DemoRequest,
    DemoRun,
    Diagnosis,
    ExecutionRecord,
    ImpactSummary,
    Incident,
    Operation,
    Receipt,
)
from packages.domain.plan import RecoveryPlan, canonical_bytes, compute_plan_digest
from packages.domain.plan_policy import PlanPolicyRejection, ReleaseDiffSnapshot, evaluate
from packages.storage.sqlite_store import SqliteControlPlaneStore
from services.executor.rollback_executor import run_execution
from services.worker.investigation_worker import run_investigation

FIXTURE_DOCUMENT_IDS = ("invoice-example-a", "invoice-example-b", "invoice-example-c")


@dataclass
class World:
    """A seeded fixture deployment plus the demo-run state built on top of it."""

    data_dir: Path
    store: SqliteControlPlaneStore
    gateway: FixtureAwsGateway
    app_config: AppConfig
    good_version: str
    bad_version: str
    demo_run_id: str = ""
    request_ids: list[str] = field(default_factory=list)
    incident_id: str = ""

    # -- workload ------------------------------------------------------
    def submit_requests(self, count: int = 3) -> list[str]:
        now = datetime.now(UTC)
        run = DemoRun(
            demo_run_id=new_id("run"),
            workspace_id=self.app_config.workspace_id,
            app_id=self.app_config.app_id,
            mode=Mode.FIXTURE,
            created_at=now,
        )
        self.store.create_demo_run(run)
        self.demo_run_id = run.demo_run_id

        for i in range(count):
            request_id = new_id("docreq")
            document = DemoDocument(
                fixture_id=FIXTURE_DOCUMENT_IDS[i % len(FIXTURE_DOCUMENT_IDS)],
                document_type="invoice",
                page_count=2,
            )
            self.store.create_demo_request(
                DemoRequest(
                    request_id=request_id,
                    demo_run_id=run.demo_run_id,
                    app_id=self.app_config.app_id,
                    document=document,
                    payload_sha256=payload_sha256(canonicalize_input(request_id, document.model_dump())),
                    status=RequestStatus.ACCEPTED,
                    created_at=now,
                    updated_at=now,
                )
            )
            self.request_ids.append(request_id)
        return self.request_ids

    def point_alias_at(self, version: str) -> str:
        """Move the alias without a revision check (owner-only fault injection)."""
        state = self.gateway.force_set_alias(self.app_config.function_name, self.app_config.alias_name, version)
        return state.alias_revision_id

    def inject_fault(self) -> str:
        return self.point_alias_at(self.bad_version)

    def register_version(
        self,
        version: str,
        *,
        results_table_ref: str | None = None,
        can_access: bool,
        is_known_good: bool = False,
    ) -> None:
        """Publish (or re-publish) one Lambda version in both the gateway and the manifest."""
        table_ref = results_table_ref or self.app_config.expected_results_table_ref
        known_good = self.store.get_known_good_deployment(self.app_config.app_id)
        assert known_good is not None
        self.store.record_deployment_version(
            DeploymentManifestEntry(
                app_id=self.app_config.app_id,
                version=version,
                results_table_ref=table_ref,
                schema_version_field=known_good.schema_version_field,
                config_fingerprint=known_good.config_fingerprint,
                is_known_good=is_known_good,
                published_at=datetime.now(UTC),
                canary_passed=is_known_good,
                processor_role_fingerprint=known_good.processor_role_fingerprint,
            )
        )
        self.gateway.register_version(
            function_name=self.app_config.function_name,
            version=version,
            results_table_ref=table_ref,
            config_fingerprint=known_good.config_fingerprint,
            can_access=can_access,
        )

    def set_version_accessible(self, version: str, can_access: bool) -> None:
        """Flip only the runtime accessibility of an already-published version."""
        entry = self.store.get_deployment_version(self.app_config.app_id, version)
        assert entry is not None
        self.gateway.register_version(
            function_name=self.app_config.function_name,
            version=version,
            results_table_ref=entry.results_table_ref,
            config_fingerprint=entry.config_fingerprint,
            can_access=can_access,
        )

    def dispatch(self) -> None:
        """Invoke the alias once per submitted request and record the outcome."""
        for request_id in self.request_ids:
            request = self.store.get_demo_request(request_id)
            result = self.gateway.invoke(
                self.app_config.function_name,
                self.app_config.alias_name,
                {"request_id": request_id, "document": request.document.model_dump()},
            )
            status = RequestStatus.FAILED if result.function_error else RequestStatus.COMPLETED
            self.store.update_demo_request(
                request_id,
                lambda r, s=status: r.model_copy(
                    update={"status": s, "attempts": r.attempts + 1, "updated_at": datetime.now(UTC)}
                ),
            )

    # -- incident ------------------------------------------------------
    def open_incident(self) -> str:
        requests = self.store.list_demo_requests(self.demo_run_id)
        failed = [r for r in requests if r.status == RequestStatus.FAILED]
        now = datetime.now(UTC)
        alias = self.gateway.get_alias_state(self.app_config.function_name, self.app_config.alias_name)
        incident = Incident(
            incident_id=new_id("inc"),
            workspace_id=self.app_config.workspace_id,
            app_id=self.app_config.app_id,
            demo_run_id=self.demo_run_id,
            fingerprint=f"{self.app_config.app_id}:v{alias.current_version}",
            state=IncidentState.DETECTED,
            mode=Mode.FIXTURE,
            version=1,
            created_at=now,
            updated_at=now,
            impact=ImpactSummary(
                failed_requests=len(failed),
                completed_requests=len(requests) - len(failed),
                first_failure_at=now if failed else None,
                mode=Mode.FIXTURE,
            ),
        )
        self.store.create_incident(incident)
        self.incident_id = incident.incident_id
        self._set_state(IncidentState.INVESTIGATING)
        return incident.incident_id

    def incident(self) -> Incident:
        return self.store.get_incident(self.incident_id)

    def receipt(self) -> Receipt | None:
        return self.store.get_latest_receipt(self.incident_id)

    def _set_state(self, state: IncidentState) -> None:
        incident = self.incident()
        self.store.update_incident(
            self.incident_id,
            incident.version,
            lambda i: i.model_copy(update={"state": state, "version": i.version + 1, "updated_at": datetime.now(UTC)}),
        )

    # -- investigation -------------------------------------------------
    def run_context(self, run_id: str | None = None) -> RunContext:
        return RunContext(
            incident_id=self.incident_id,
            run_id=run_id or new_id("op"),
            app_id=self.app_config.app_id,
            demo_run_id=self.demo_run_id,
            workspace_id=self.app_config.workspace_id,
            mode="fixture",
            data_dir=str(self.data_dir),
        )

    def investigate(self) -> Diagnosis:
        operation_id = new_id("op")
        now = datetime.now(UTC)
        self.store.create_operation(
            Operation(
                operation_id=operation_id,
                kind=OperationKind.INVESTIGATION,
                incident_id=self.incident_id,
                status=OperationStatus.QUEUED,
                created_at=now,
                updated_at=now,
            )
        )
        run_investigation(
            self.store,
            self.gateway,
            incident_id=self.incident_id,
            operation_id=operation_id,
            mode="fixture",
            data_dir=str(self.data_dir),
            agent_mode="stub",
        )
        return self.store.get_latest_diagnosis(self.incident_id)

    # -- plan / approval / execution -----------------------------------
    def release_diff(self) -> dict:
        response = evidence_tools.get_release_diff(self.run_context(), self.store, self.gateway, self.incident_id)
        assert response["status"] == "ok", response
        return response["data"]

    def diff_snapshot(self) -> ReleaseDiffSnapshot:
        data = self.release_diff()
        return ReleaseDiffSnapshot(
            alias_name=data["alias_name"],
            current_version=data["current_version"],
            known_good_version=data["known_good_version"],
            alias_revision_id=data["alias_revision_id"],
            weighted_routing=data["weighted_routing"],
            schema_compatible=data["schema_compatible"],
            changes=data["changes"],
            processor_role_matches_manifest=data["processor_role_matches_manifest"],
        )

    def build_plan(
        self,
        *,
        replay_request_ids: list[str] | None = None,
        now: datetime | None = None,
        action: RecommendedAction | None = None,
    ) -> RecoveryPlan | PlanPolicyRejection:
        """Run the deterministic gate over fresh evidence.

        ``action`` overrides the stored diagnosis so a test can ask "would the
        policy still refuse even if the agent *had* recommended a rollback?".
        """
        diagnosis = self.store.get_latest_diagnosis(self.incident_id)
        selected = self.request_ids if replay_request_ids is None else replay_request_ids
        return evaluate(
            incident_id=self.incident_id,
            app_config=self.app_config,
            diagnosis_recommended_action=action or diagnosis.recommended_action,
            diagnosis_evidence_ids=diagnosis.evidence_ids,
            diff=self.diff_snapshot(),
            known_good_entry=self.store.get_known_good_deployment(self.app_config.app_id),
            replay_request_ids=[(r, self.store.get_demo_request(r).payload_sha256) for r in selected],
            now=now,
        )

    def plan(self) -> RecoveryPlan:
        """The plan the investigation worker itself built and attached to the incident."""
        plan_id = self.incident().active_plan_id
        assert plan_id is not None, "no plan was attached to this incident"
        return self.store.get_plan(plan_id)

    def store_plan(self, plan: RecoveryPlan) -> str:
        """Persist a test-built plan; a worker-built plan is already stored."""
        existing = self.store.get_plan_digest(plan.plan_id)
        if existing is not None:
            return existing
        digest = compute_plan_digest(plan)
        self.store.put_plan(plan, canonical_bytes(plan), digest)
        return digest

    def approve(self, plan: RecoveryPlan, digest: str) -> Approval:
        approval = Approval(
            approval_id=new_id("approval"),
            plan_id=plan.plan_id,
            plan_digest=digest,
            incident_id=self.incident_id,
            actor_id="local-owner",
            decision=Decision.APPROVE,
            decided_at=datetime.now(UTC),
            plan_expires_at=plan.expires_at,
        )
        self.store.put_approval(approval)
        return approval

    def queue_execution(self, plan: RecoveryPlan, approval: Approval) -> str:
        operation_id = new_id("op")
        now = datetime.now(UTC)
        self.store.create_operation(
            Operation(
                operation_id=operation_id,
                kind=OperationKind.EXECUTION,
                incident_id=self.incident_id,
                status=OperationStatus.QUEUED,
                created_at=now,
                updated_at=now,
            )
        )
        self.store.put_execution_record(
            ExecutionRecord(
                operation_id=operation_id,
                plan_id=plan.plan_id,
                approval_id=approval.approval_id,
                incident_id=self.incident_id,
            )
        )
        return operation_id

    def execute(self, operation_id: str) -> Operation:
        run_execution(self.store, self.gateway, operation_id=operation_id)
        return self.store.get_operation(operation_id)

    def approve_and_execute(
        self,
        plan: RecoveryPlan,
        *,
        digest: str | None = None,
    ) -> Operation:
        stored_digest = self.store_plan(plan)
        approval = self.approve(plan, digest or stored_digest)
        return self.execute(self.queue_execution(plan, approval))

    # -- assertions helpers --------------------------------------------
    def alias_version(self) -> str:
        return self.gateway.get_alias_state(self.app_config.function_name, self.app_config.alias_name).current_version

    def needs_attention_codes(self) -> list[str]:
        page = self.store.list_events(self.incident_id, after_sequence=0, limit=200)
        return [e.data.get("code") for e in page.items if e.type == "incident.needs_attention"]

    def result_row_count(self, request_id: str) -> int:
        """Count durable result rows across every simulated results table."""
        with sqlite3.connect(self.data_dir / "fixture_aws.db") as conn:
            return conn.execute("SELECT COUNT(*) FROM results_table WHERE request_id=?", (request_id,)).fetchone()[0]

    def log_line(self, message: str, *, error_type: str = "ResultsTableUnavailable") -> None:
        """Append an attacker-controlled CloudWatch line (the gateway's own writer)."""
        self.gateway._log(self.app_config.function_name, request_id=None, error_type=error_type, message=message)


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A freshly seeded deployment: G and B published, alias pointing at G."""
    data_dir = tmp_path / "data"
    seed_fixture.seed(data_dir)

    store = SqliteControlPlaneStore(data_dir / "control_plane.db")
    gateway = FixtureAwsGateway(data_dir / "fixture_aws.db")
    app_config = store.get_app_config("document-demo")
    assert app_config is not None

    entries = seed_fixture.load_deployment_entries()
    good = next(e for e in entries if e.is_known_good)
    bad = next(e for e in entries if not e.is_known_good)
    return World(
        data_dir=data_dir,
        store=store,
        gateway=gateway,
        app_config=app_config,
        good_version=good.version,
        bad_version=bad.version,
    )


@pytest.fixture
def broken_world(world: World) -> World:
    """Three requests submitted, version B live, all three failed, incident open."""
    world.submit_requests(3)
    world.inject_fault()
    world.dispatch()
    world.open_incident()
    return world


@pytest.fixture
def investigated_world(broken_world: World) -> World:
    """``broken_world`` after a supported diagnosis -> awaiting_approval."""
    broken_world.investigate()
    return broken_world
