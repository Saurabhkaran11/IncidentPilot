"""The trusted run context an MCP evidence server process is bound to.

Per build brief section 12: "Bind each process/session to a trusted run
context; reject incident IDs outside it." The worker spawns one MCP server
subprocess per investigation and passes this context through environment
variables -- never through a tool argument the model could forge, and never
by letting the model choose account/region/app (build brief section 8).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class RunContextError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunContext:
    incident_id: str
    run_id: str
    app_id: str
    demo_run_id: str
    workspace_id: str
    mode: str  # "fixture" | "aws_live"
    data_dir: str

    @classmethod
    def from_env(cls) -> RunContext:
        try:
            return cls(
                incident_id=os.environ["INCIDENTPILOT_INCIDENT_ID"],
                run_id=os.environ["INCIDENTPILOT_RUN_ID"],
                app_id=os.environ["INCIDENTPILOT_APP_ID"],
                demo_run_id=os.environ["INCIDENTPILOT_DEMO_RUN_ID"],
                workspace_id=os.environ["INCIDENTPILOT_WORKSPACE_ID"],
                mode=os.environ.get("INCIDENTPILOT_MODE", "fixture"),
                data_dir=os.environ.get("INCIDENTPILOT_DATA_DIR", "data"),
            )
        except KeyError as exc:
            raise RunContextError(
                f"missing required env var {exc}; the MCP server must be spawned by services/worker"
            ) from exc

    def in_scope(self, incident_id: str) -> bool:
        return incident_id == self.incident_id
