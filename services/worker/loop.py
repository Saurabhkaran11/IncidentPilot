"""The local worker loop: drains every queued operation kind.

Fixture and local-live only. The hosted deployment deletes this loop
entirely -- each operation kind becomes an SQS queue with its own worker
Lambda and its own IAM role, which is the whole point of keeping
investigation and execution in *separate* queues: the investigator's role
must never be able to assume the executor's (build brief section 7).

Single-process by design. There is no atomic claim between "list queued"
and "mark running", so running two of these against one SQLite file can
double-process an operation. SQS's visibility timeout is what provides that
guarantee in the hosted path; here, run one.
"""

from __future__ import annotations

import logging
import time

from packages.aws.gateway import AwsGateway
from packages.domain.enums import Mode, OperationKind, OperationStatus
from packages.storage.interface import ControlPlaneStore
from services.executor.rollback_executor import run_execution
from services.worker.dispatch_worker import run_dispatch
from services.worker.investigation_worker import run_investigation

logger = logging.getLogger("incidentpilot.worker.loop")


def drain_once(
    store: ControlPlaneStore,
    gateway: AwsGateway,
    *,
    mode: Mode,
    data_dir: str,
    agent_mode: str,
) -> int:
    """Run every currently-queued operation. Returns how many it handled.

    Ordering is deliberate: process customer requests first (they create
    incidents), then investigate, then execute. One pass therefore advances
    a whole demo without waiting for three poll intervals.
    """
    handled = 0

    for op in store.list_operations_by_status(OperationKind.PROCESSING.value, OperationStatus.QUEUED.value, limit=10):
        # PROCESSING operations carry the request id in `incident_id`
        # (there is no incident yet when the request is submitted).
        run_dispatch(store, gateway, request_id=op.incident_id, operation_id=op.operation_id, mode=mode)
        handled += 1

    for op in store.list_operations_by_status(OperationKind.INVESTIGATION.value, OperationStatus.QUEUED.value, limit=5):
        run_investigation(
            store, gateway, incident_id=op.incident_id, operation_id=op.operation_id,
            mode=mode.value, data_dir=data_dir, agent_mode=agent_mode,
        )
        handled += 1

    for op in store.list_operations_by_status(OperationKind.EXECUTION.value, OperationStatus.QUEUED.value, limit=5):
        run_execution(store, gateway, operation_id=op.operation_id)
        handled += 1

    return handled


def run_forever(
    store: ControlPlaneStore,
    gateway: AwsGateway,
    *,
    mode: Mode,
    data_dir: str,
    agent_mode: str,
    poll_seconds: float = 1.0,
) -> None:
    logger.info("worker started (mode=%s agent_mode=%s)", mode.value, agent_mode)
    while True:
        try:
            if drain_once(store, gateway, mode=mode, data_dir=data_dir, agent_mode=agent_mode) == 0:
                time.sleep(poll_seconds)
        except KeyboardInterrupt:
            logger.info("worker stopped")
            return
        except Exception:
            # One poisoned operation must not kill the loop; it is already
            # marked needs_attention by the handler that raised.
            logger.exception("worker iteration failed")
            time.sleep(poll_seconds)
