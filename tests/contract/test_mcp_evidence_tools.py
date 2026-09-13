"""Every evidence tool response, validated against its published JSON Schema.

The schemas come from ``contracts/mcp/evidence-tools.json`` -- the file the
MCP server advertises -- not from a copy maintained inside the tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from mcp_server import tools as evidence_tools
from packages.domain.enums import McpErrorCode

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contracts" / "mcp" / "evidence-tools.json"
TOOLS = {tool["name"]: tool for tool in json.loads(CONTRACT_PATH.read_text())["tools"]}
TOOL_NAMES = sorted(TOOLS)


def validate(tool_name: str, response: dict) -> None:
    Draft202012Validator(TOOLS[tool_name]["outputSchema"]).validate(response)


def call(tool_name: str, world, incident_id: str) -> dict:
    ctx = world.run_context()
    dispatch = {
        "get_incident_context": lambda: evidence_tools.get_incident_context(ctx, world.store, incident_id),
        "get_error_evidence": lambda: evidence_tools.get_error_evidence(
            ctx, world.store, world.gateway, incident_id
        ),
        "get_release_diff": lambda: evidence_tools.get_release_diff(
            ctx, world.store, world.gateway, incident_id
        ),
        "get_pending_request_summary": lambda: evidence_tools.get_pending_request_summary(
            ctx, world.store, incident_id
        ),
        "get_runbook": lambda: evidence_tools.get_runbook(
            ctx, world.store, incident_id, "document-alias-rollback-v1"
        ),
    }
    return dispatch[tool_name]()


def test_the_contract_publishes_exactly_the_five_evidence_tools():
    assert TOOL_NAMES == [
        "get_error_evidence",
        "get_incident_context",
        "get_pending_request_summary",
        "get_release_diff",
        "get_runbook",
    ]


@pytest.mark.parametrize("tool_name", TOOL_NAMES)
def test_an_ok_response_matches_the_published_schema(tool_name, investigated_world):
    response = call(tool_name, investigated_world, investigated_world.incident_id)

    assert response["status"] == "ok"
    assert response["data"] is not None
    assert response["error"] is None
    validate(tool_name, response)


@pytest.mark.parametrize("tool_name", TOOL_NAMES)
def test_an_incident_outside_the_bound_run_context_is_refused(tool_name, investigated_world):
    response = call(tool_name, investigated_world, "inc_someone_elses_incident")

    assert response["status"] == "error"
    assert response["error"]["code"] == McpErrorCode.RESOURCE_OUT_OF_SCOPE.value
    assert response["data"] is None
    assert response["evidence"] == []
    validate(tool_name, response)


def test_an_unknown_runbook_id_is_an_invalid_argument_error(investigated_world):
    response = evidence_tools.get_runbook(
        investigated_world.run_context(),
        investigated_world.store,
        investigated_world.incident_id,
        "attacker-supplied-runbook",
    )

    assert response["error"]["code"] == McpErrorCode.INVALID_ARGUMENT.value
    validate("get_runbook", response)


@pytest.mark.parametrize("tool_name", TOOL_NAMES)
def test_the_schema_rejects_an_error_envelope_that_still_carries_data(tool_name, investigated_world):
    """The allOf rule is enforcement, not decoration."""
    response = call(tool_name, investigated_world, investigated_world.incident_id)
    response["status"] = "error"

    with pytest.raises(ValidationError):
        validate(tool_name, response)


@pytest.mark.parametrize("tool_name", TOOL_NAMES)
def test_the_schema_rejects_an_ok_envelope_that_carries_an_error(tool_name, investigated_world):
    response = call(tool_name, investigated_world, "inc_someone_elses_incident")
    response["status"] = "ok"

    with pytest.raises(ValidationError):
        validate(tool_name, response)
