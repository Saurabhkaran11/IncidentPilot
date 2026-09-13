#!/usr/bin/env python
"""Seed fixture mode: register the demo app, publish G and B, point the alias at G.

Run once before starting the API in fixture mode (``services/api/main.py``
calls this automatically on startup if the control-plane DB is empty, but it
is also safe -- and useful for tests -- to run directly):

    python scripts/seed_fixture.py [--data-dir data]

This reproduces build brief section 5 steps 1-2 (deploy G, publish B) using
the same ``FixtureAwsGateway``/``SqliteControlPlaneStore`` the running
server uses -- not a separate mock, so a passing seed script means the
server will see the identical state.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from packages.aws.fixture_gateway import FixtureAwsGateway
from packages.domain.config import AppConfig, DeploymentManifestEntry
from packages.storage.sqlite_store import SqliteControlPlaneStore

MANIFEST_DIR = Path(__file__).resolve().parent.parent / "demo" / "manifests"


def load_app_config() -> AppConfig:
    return AppConfig.model_validate_json((MANIFEST_DIR / "document-demo.app.json").read_text())


def load_deployment_entries() -> list[DeploymentManifestEntry]:
    raw = json.loads((MANIFEST_DIR / "document-demo.deployments.json").read_text())
    return [DeploymentManifestEntry.model_validate(item) for item in raw]


def seed(data_dir: Path) -> None:
    control_plane = SqliteControlPlaneStore(data_dir / "control_plane.db")
    gateway = FixtureAwsGateway(data_dir / "fixture_aws.db")

    app_config = load_app_config()
    control_plane.put_app_config(app_config)

    entries = load_deployment_entries()
    good = next(e for e in entries if e.is_known_good)
    bad = next(e for e in entries if not e.is_known_good)

    for entry in entries:
        control_plane.record_deployment_version(entry)
        gateway.register_version(
            function_name=app_config.function_name,
            version=entry.version,
            results_table_ref=entry.results_table_ref,
            config_fingerprint=entry.config_fingerprint,
            can_access=(entry.results_table_ref == app_config.expected_results_table_ref),
        )

    gateway.force_set_alias(app_config.function_name, app_config.alias_name, good.version)

    print(f"Seeded app_config={app_config.app_id}")
    print(f"  known-good version = {good.version} -> {good.results_table_ref}")
    print(f"  faulty version     = {bad.version} -> {bad.results_table_ref} (access denied)")
    state = gateway.get_alias_state(app_config.function_name, app_config.alias_name)
    print(f"  alias {state.alias_name!r} now points at version {state.current_version} "
          f"(revision {state.alias_revision_id})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data", help="directory for the sqlite files")
    args = parser.parse_args()
    seed(Path(args.data_dir))


if __name__ == "__main__":
    main()
