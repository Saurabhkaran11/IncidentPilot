"""Process configuration: the one place "fixture vs live" is decided.

Everything downstream receives an already-chosen ``ControlPlaneStore`` and
``AwsGateway`` (build brief section 7). No route handler ever branches on
mode to pick a backend -- if you find yourself writing ``if mode ==`` in a
handler, the dependency belongs here instead.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from packages.domain.enums import AgentMode, Mode


@dataclass(frozen=True)
class Settings:
    mode: Mode
    agent_mode: AgentMode
    data_dir: Path
    ui_origin: str
    workspace_id: str
    app_id: str
    #: Fixture mode only: the fixed development actor, valid solely on
    #: loopback (API contract, "Shared conventions"). Hosted mode rejects it.
    dev_actor_id: str
    token_ttl_seconds: int = 30 * 60

    @property
    def is_live(self) -> bool:
        return self.mode is Mode.AWS_LIVE


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    mode = Mode(os.environ.get("INCIDENTPILOT_MODE", "fixture"))
    agent_mode_raw = os.environ.get("INCIDENTPILOT_AGENT_MODE")
    if agent_mode_raw:
        agent_mode = AgentMode(agent_mode_raw)
    else:
        # Default honestly: only claim "bedrock" when someone configured it.
        agent_mode = AgentMode.STUB
    return Settings(
        mode=mode,
        agent_mode=agent_mode,
        data_dir=Path(os.environ.get("INCIDENTPILOT_DATA_DIR", "data")),
        ui_origin=os.environ.get("INCIDENTPILOT_UI_ORIGIN", "http://127.0.0.1:5173"),
        workspace_id=os.environ.get("INCIDENTPILOT_WORKSPACE_ID", "workspace_demo"),
        app_id=os.environ.get("INCIDENTPILOT_APP_ID", "document-demo"),
        dev_actor_id="local-dev-operator",
    )


@lru_cache(maxsize=1)
def get_store():
    from packages.storage.sqlite_store import SqliteControlPlaneStore

    return SqliteControlPlaneStore(get_settings().data_dir / "control_plane.db")


@lru_cache(maxsize=1)
def get_gateway():
    settings = get_settings()
    if settings.is_live:
        from packages.aws.live_gateway import LiveAwsGateway

        return LiveAwsGateway()
    from packages.aws.fixture_gateway import FixtureAwsGateway

    return FixtureAwsGateway(settings.data_dir / "fixture_aws.db")
