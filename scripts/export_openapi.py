#!/usr/bin/env python
"""Export the implemented API's OpenAPI document and check it against the contract.

    python scripts/export_openapi.py            # write + verify
    python scripts/export_openapi.py --check    # verify only (CI); non-zero on drift

The build brief asks for OpenAPI generated *from the implementation* and
validated against ``docs/specs/IncidentPilot-API-Contract.md`` -- not
hand-written alongside it. So the document is generated from the live
FastAPI app, and the endpoint table in that Markdown contract is parsed and
compared method-by-method. Drift in either direction is reported:

* contract endpoints with no implementation -> not built yet
* implemented endpoints absent from the contract -> the contract needs an
  update (and an explicit note in docs/BUILD_STATUS.md)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
CONTRACT_MD = ROOT / "docs" / "specs" / "IncidentPilot-API-Contract.md"
OUTPUT = ROOT / "contracts" / "openapi" / "incidentpilot.openapi.json"

#: Rows look like: | `GET /healthz` | Public | Process readiness... |
_ROW = re.compile(r"^\|\s*`(GET|POST|PUT|PATCH|DELETE)\s+([^`]+)`\s*\|")


def contract_endpoints() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for line in CONTRACT_MD.read_text(encoding="utf-8").splitlines():
        match = _ROW.match(line.strip())
        if match:
            found.add((match.group(1).upper(), match.group(2).strip()))
    return found


def implemented_endpoints(spec: dict) -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, operations in spec["paths"].items()
        for method in operations
        if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    }


def generate_spec() -> dict:
    os.environ.setdefault("INCIDENTPILOT_MODE", "fixture")
    os.environ.setdefault("INCIDENTPILOT_DATA_DIR", "data")
    from services.api.main import create_app

    return create_app().openapi()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail instead of writing when the file would change")
    args = parser.parse_args()

    spec = generate_spec()
    rendered = json.dumps(spec, indent=2, sort_keys=True) + "\n"

    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != rendered:
            print("OpenAPI document is stale; run: python scripts/export_openapi.py", file=sys.stderr)
            return 1
    else:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(rendered, encoding="utf-8")
        print(f"wrote {OUTPUT.relative_to(ROOT)} ({len(rendered)} bytes)")

    contract = contract_endpoints()
    implemented = implemented_endpoints(spec)

    missing = sorted(contract - implemented)
    extra = sorted(implemented - contract)

    print(f"\ncontract endpoints:    {len(contract)}")
    print(f"implemented endpoints: {len(implemented)}")

    if missing:
        print("\nIn the contract but NOT implemented:")
        for method, path in missing:
            print(f"  - {method} {path}")
    if extra:
        print("\nImplemented but NOT in the contract:")
        for method, path in extra:
            print(f"  + {method} {path}")
    if not missing and not extra:
        print("\nEvery contract endpoint is implemented, and nothing extra is exposed.")

    # Drift is reported, not fatal: docs/BUILD_STATUS.md is the place that
    # records what is deliberately unbuilt. Only --check gates CI, and only
    # on the generated document being stale.
    return 0


if __name__ == "__main__":
    sys.exit(main())
