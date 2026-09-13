"""Trusted configuration: the registered application and its deployment history.

None of this is ever accepted from an HTTP request body or from the model.
It is loaded from ``demo/manifests/*.json`` (fixture mode) or written by the
setup scripts after a real ``aws lambda publish-version`` call (live mode)
-- see docs/decisions/0001-single-agent-read-only-mcp.md and build brief
section 8: "Account, region, application, permitted resources, time window,
and tools come from trusted configuration, not the model or request text."
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from packages.domain.types import NonEmptyStr, Sha256Hex


class AppConfig(BaseModel):
    """The one registered demo application (build brief section 5)."""

    model_config = ConfigDict(extra="forbid")

    app_id: str
    workspace_id: str
    label: str
    region: NonEmptyStr
    account_id: NonEmptyStr
    function_name: NonEmptyStr
    alias_name: NonEmptyStr = "live"
    log_group_name: NonEmptyStr
    expected_results_table_ref: NonEmptyStr
    forbidden_results_table_ref: NonEmptyStr
    processor_role_fingerprint: Sha256Hex
    runbook_id: Literal["document-alias-rollback-v1"] = "document-alias-rollback-v1"
    schema_version: str = "1.0"


class DeploymentManifestEntry(BaseModel):
    """One published Lambda version, recorded at publish time.

    ``config_fingerprint`` is ``sha256`` of the allowlisted config fields
    (currently just ``RESULTS_TABLE`` + ``SCHEMA_VERSION``) -- never a hash
    of the full environment, which could include secrets.
    """

    model_config = ConfigDict(extra="forbid")

    app_id: str
    version: NonEmptyStr
    results_table_ref: NonEmptyStr
    schema_version_field: str = "1.0"
    config_fingerprint: Sha256Hex
    is_known_good: bool
    published_at: datetime
    canary_passed: bool
    canary_verified_at: datetime | None = None
    processor_role_fingerprint: Sha256Hex


class AliasState(BaseModel):
    """Current pointer state of the registered alias.

    In fixture mode this is simulated storage-side state (see
    ``demo/workload/fixture_gateway.py``). In live mode it is read straight
    from ``lambda:GetAlias`` and never cached, per build brief section 10
    ("Immediately before mutation, the executor validates ... the live alias
    revision").
    """

    model_config = ConfigDict(extra="forbid")

    function_name: NonEmptyStr
    alias_name: NonEmptyStr
    current_version: NonEmptyStr
    alias_revision_id: NonEmptyStr
    weighted_routing: bool = False
    updated_at: datetime
