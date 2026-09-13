#!/usr/bin/env python
"""Run the control API on loopback.

    python scripts/run_api.py                    # fixture mode, no AWS needed
    INCIDENTPILOT_MODE=aws_live python scripts/run_api.py

Binds 127.0.0.1 only. In ``aws_live`` mode the server performs an STS
identity check at startup and prints a single 30-minute operator token to
this terminal -- that token is the only way to call a mutating route, and a
restart invalidates it (build brief section 7).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        "services.api.main:app",
        # Loopback only. This is a single-operator local demo, not a hosted
        # service; binding 0.0.0.0 would expose approval routes protected
        # only by a local dev actor.
        host="127.0.0.1",
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
