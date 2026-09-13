#!/usr/bin/env python
"""``incidentpilot-evidence``: the read-only MCP stdio server Strands consumes.

Registers exactly the five tools in ``contracts/mcp/evidence-tools.json``,
using their JSON Schemas *verbatim* (loaded from that file, never
retyped) for ``inputSchema``/``outputSchema``/``annotations`` -- see build
brief section 12. Spawned once per investigation by
``services/worker/investigation_worker.py`` with a bound ``RunContext``
(env vars); every tool call for a different ``incident_id`` is refused with
``RESOURCE_OUT_OF_SCOPE`` regardless of what the model asks for.

No execution or approval authority is reachable from here: this process
never imports ``services/executor`` and its AWS session (fixture or live)
is read-only by construction (``FixtureAwsGateway``/``LiveAwsGateway`` never
receive executor credentials -- see docs/decisions/0004-separate-executor-identity.md).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from mcp_server import tools as tool_impl
from mcp_server.context import RunContext, RunContextError

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("incidentpilot.mcp_server")

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contracts" / "mcp" / "evidence-tools.json"
TOOL_CALL_TIMEOUT_SECONDS = 15  # build brief section 8: "15 seconds per evidence tool"


def _load_tool_specs() -> dict[str, dict[str, Any]]:
    raw = json.loads(CONTRACT_PATH.read_text())
    return {t["name"]: t for t in raw["tools"]}


def _build_gateway(ctx: RunContext):
    if ctx.mode == "aws_live":
        from packages.aws.live_gateway import LiveAwsGateway

        return LiveAwsGateway()
    from packages.aws.fixture_gateway import FixtureAwsGateway

    return FixtureAwsGateway(Path(ctx.data_dir) / "fixture_aws.db")


def _build_store(ctx: RunContext):
    from packages.storage.sqlite_store import SqliteControlPlaneStore

    return SqliteControlPlaneStore(Path(ctx.data_dir) / "control_plane.db")


def build_server() -> Server:
    specs = _load_tool_specs()
    ctx = RunContext.from_env()
    store = _build_store(ctx)
    gateway = _build_gateway(ctx)

    server: Server = Server("incidentpilot-evidence")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=spec["name"],
                description=spec["description"],
                inputSchema=spec["inputSchema"],
                outputSchema=spec["outputSchema"],
                annotations=types.ToolAnnotations(**spec["annotations"]),
            )
            for spec in specs.values()
        ]

    def _dispatch(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        incident_id = arguments.get("incident_id", "")
        if name == "get_incident_context":
            return tool_impl.get_incident_context(ctx, store, incident_id)
        if name == "get_error_evidence":
            return tool_impl.get_error_evidence(
                ctx, store, gateway, incident_id,
                window_minutes=arguments.get("window_minutes", 15),
                limit=arguments.get("limit", 50),
            )
        if name == "get_release_diff":
            return tool_impl.get_release_diff(ctx, store, gateway, incident_id)
        if name == "get_pending_request_summary":
            return tool_impl.get_pending_request_summary(ctx, store, incident_id)
        if name == "get_runbook":
            return tool_impl.get_runbook(ctx, store, incident_id, arguments.get("runbook_id", ""))
        raise KeyError(f"unknown tool {name!r}")

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(_dispatch, name, arguments), timeout=TOOL_CALL_TIMEOUT_SECONDS
            )
            is_error = result.get("status") == "error"
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(result))],
                structuredContent=result,
                isError=is_error,
            )
        except TimeoutError:
            payload = {
                "schema_version": "1.0",
                "status": "error",
                "data": None,
                "evidence": [],
                "truncated": False,
                "warnings": [],
                "error": {"code": "TIMEOUT", "message": f"{name} exceeded {TOOL_CALL_TIMEOUT_SECONDS}s", "retryable": True},
            }
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(payload))],
                structuredContent=payload,
                isError=True,
            )
        except Exception as exc:  # last-resort: never crash the server on one bad call
            logger.exception("tool %s failed", name)
            payload = {
                "schema_version": "1.0",
                "status": "error",
                "data": None,
                "evidence": [],
                "truncated": False,
                "warnings": [],
                "error": {"code": "INTERNAL_ERROR", "message": str(exc), "retryable": False},
            }
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(payload))],
                structuredContent=payload,
                isError=True,
            )

    return server


async def run() -> None:
    try:
        server = build_server()
    except RunContextError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="incidentpilot-evidence",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(), experimental_capabilities={}
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(run())
