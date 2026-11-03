#!/bin/sh
# Public walkthrough of the simulation: the API and web app, plus the worker that drains their queue.
set -e
export INCIDENTPILOT_PUBLIC_DEMO=1 INCIDENTPILOT_MODE=fixture INCIDENTPILOT_AGENT_MODE=stub
export INCIDENTPILOT_DATA_DIR="${INCIDENTPILOT_DATA_DIR:-/tmp/incidentpilot}"
python -m scripts.seed_fixture --data-dir "$INCIDENTPILOT_DATA_DIR"
python scripts/run_worker.py &
exec python -m uvicorn services.api.main:app --host 0.0.0.0 --port "${PORT:-8080}"
