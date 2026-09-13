#!/usr/bin/env python
"""Owner-only fault injection: point the demo alias at the faulty version.

    python scripts/inject_fault.py                      # fixture mode
    INCIDENTPILOT_MODE=aws_live python scripts/inject_fault.py --version 7

This is a CLI command, never an HTTP endpoint. Nothing a public visitor or
an authenticated operator can reach through the API can break the demo
application (API contract: "Fault injection is an owner-only CLI command,
not an anonymous HTTP endpoint"). That asymmetry is deliberate: the product
is allowed to *recover* the workload, and only its owner is allowed to
*break* it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packages.domain.enums import Mode  # noqa: E402
from services.api.config import get_gateway, get_settings, get_store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="version to point the alias at (default: the recorded faulty version)")
    parser.add_argument("--restore", action="store_true", help="point the alias back at the known-good version")
    args = parser.parse_args()

    settings = get_settings()
    store = get_store()
    gateway = get_gateway()

    app_config = store.get_app_config(settings.app_id)
    if app_config is None:
        print(f"application {settings.app_id!r} is not registered; run scripts/seed_fixture.py first", file=sys.stderr)
        return 1

    if args.version:
        target = args.version
    elif args.restore:
        known_good = store.get_known_good_deployment(app_config.app_id)
        if known_good is None:
            print("no known-good version recorded", file=sys.stderr)
            return 1
        target = known_good.version
    else:
        faulty = [
            entry
            for version in _known_versions(store, app_config.app_id)
            if (entry := store.get_deployment_version(app_config.app_id, version)) and not entry.is_known_good
        ]
        if not faulty:
            print("no faulty version recorded; pass --version explicitly", file=sys.stderr)
            return 1
        target = faulty[0].version

    before = gateway.get_alias_state(app_config.function_name, app_config.alias_name)

    if settings.mode is Mode.FIXTURE:
        after = gateway.force_set_alias(app_config.function_name, app_config.alias_name, target)
    else:
        # Live mode: go through UpdateAlias with the observed revision as a
        # precondition, so this command cannot clobber a concurrent change
        # either -- the same rule the executor follows.
        after = gateway.update_alias(
            app_config.function_name, app_config.alias_name, before.alias_revision_id, target
        )

    print(f"mode: {settings.mode.value}")
    print(f"alias {app_config.alias_name!r}: version {before.current_version} -> {after.current_version}")
    print(f"  results table now: {after.results_table_ref}")
    print(f"  alias revision:    {after.alias_revision_id}")
    return 0


def _known_versions(store, app_id: str) -> list[str]:
    """Deployment versions recorded in the trusted manifest."""
    import json

    manifest = Path(__file__).resolve().parent.parent / "demo" / "manifests" / "document-demo.deployments.json"
    return [entry["version"] for entry in json.loads(manifest.read_text()) if entry["app_id"] == app_id]


if __name__ == "__main__":
    sys.exit(main())
