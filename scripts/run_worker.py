#!/usr/bin/env python
"""Run the local worker: drains processing, investigation, and execution work.

    python scripts/run_worker.py                 # fixture mode
    INCIDENTPILOT_MODE=aws_live python scripts/run_worker.py

Run this alongside ``scripts/run_api.py``. They are separate processes on
purpose: the API must never perform recovery work inside a request, and in
the hosted deployment these become separately-permissioned Lambdas.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.api.config import get_gateway, get_settings, get_store  # noqa: E402
from services.worker.loop import drain_once, run_forever  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="drain the queue once and exit (useful in tests/CI)")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = get_settings()
    store, gateway = get_store(), get_gateway()

    kwargs = {"mode": settings.mode, "data_dir": str(settings.data_dir), "agent_mode": settings.agent_mode.value}
    if args.once:
        handled = drain_once(store, gateway, **kwargs)
        print(f"drained {handled} operation(s)")
        return 0
    run_forever(store, gateway, poll_seconds=args.poll_seconds, **kwargs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
