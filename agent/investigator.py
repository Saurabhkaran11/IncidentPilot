"""``agent_mode: "bedrock"`` -- the real Strands investigator.

Spawns ``mcp_server/server.py`` as a bound-context stdio subprocess (one per
investigation -- see ``mcp_server/context.py``), gives the agent only those
five read-only tools, enforces the tool-call/time budget via
``agent/budget_hook.py``, and requests validated structured output matching
``agent/schema.DiagnosisOut``.

**Status: implemented, not yet exercised against live Amazon Bedrock** -- see
docs/BUILD_STATUS.md. Requires ``aws configure`` (or equivalent) with Bedrock
InvokeModel access to the configured model ID in the target region; without
that, use ``agent_mode: "stub"`` (``agent/stub_investigator.py``), which
implements the identical decision policy deterministically for CI, tests,
and demos where Bedrock access isn't available.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from mcp import StdioServerParameters, stdio_client
from strands import Agent
from strands.models import BedrockModel

from agent.budget_hook import InvestigationBudgetHook
from agent.prompt import PROMPT_VERSION, SYSTEM_PROMPT
from agent.schema import DiagnosisOut
from mcp_server.context import RunContext

MAX_TOOL_CALLS = 12
MAX_DURATION_SECONDS = 90

#: Verify this against Bedrock model access actually granted in the target
#: account/region before a live demo -- see docs/decisions/0008-model-selection.md.
DEFAULT_MODEL_ID = os.environ.get(
    "INCIDENTPILOT_BEDROCK_MODEL_ID", "anthropic.claude-3-5-sonnet-20241022-v2:0"
)


class InvestigatorError(RuntimeError):
    pass


@dataclass
class InvestigationResult:
    diagnosis: DiagnosisOut
    tool_call_count: int
    duration_ms: int
    model_id: str
    prompt_version: str
    input_tokens: int | None
    output_tokens: int | None


def _mcp_server_params(ctx: RunContext) -> StdioServerParameters:
    env = {
        **os.environ,
        "INCIDENTPILOT_INCIDENT_ID": ctx.incident_id,
        "INCIDENTPILOT_RUN_ID": ctx.run_id,
        "INCIDENTPILOT_APP_ID": ctx.app_id,
        "INCIDENTPILOT_DEMO_RUN_ID": ctx.demo_run_id,
        "INCIDENTPILOT_WORKSPACE_ID": ctx.workspace_id,
        "INCIDENTPILOT_MODE": ctx.mode,
        "INCIDENTPILOT_DATA_DIR": ctx.data_dir,
    }
    return StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.server"], env=env)


def _extract_usage(result: object) -> tuple[int | None, int | None]:
    """Best-effort token usage extraction across Strands result shapes.

    Different Strands versions have exposed usage as ``result.metrics``,
    ``result.usage``, or nested under ``result.metrics.accumulated_usage``.
    Never let a shape mismatch fail the investigation -- usage is
    reported-if-available (receipt schema: "Unknown measurements are null"),
    never required.
    """
    for attr_path in ("metrics.accumulated_usage", "metrics.usage", "usage"):
        obj = result
        try:
            for part in attr_path.split("."):
                obj = getattr(obj, part)
            input_tokens = getattr(obj, "inputTokens", None) or getattr(obj, "input_tokens", None)
            output_tokens = getattr(obj, "outputTokens", None) or getattr(obj, "output_tokens", None)
            if input_tokens is not None or output_tokens is not None:
                return input_tokens, output_tokens
        except AttributeError:
            continue
    return None, None


def investigate(ctx: RunContext, incident_id: str, *, model_id: str | None = None) -> InvestigationResult:
    """Run one bounded investigation. Raises ``InvestigatorError`` on failure.

    Callers (``services/worker``) must themselves enforce the outer 90s
    wall-clock deadline (e.g. via a thread/future timeout) -- the budget
    hook cancels further *tool calls* once the clock runs out, but cannot
    interrupt a single long-running model call already in flight.
    """
    from strands.tools.mcp import MCPClient

    resolved_model_id = model_id or DEFAULT_MODEL_ID
    budget = InvestigationBudgetHook(MAX_TOOL_CALLS, MAX_DURATION_SECONDS)
    mcp_client = MCPClient(lambda: stdio_client(_mcp_server_params(ctx)))

    with mcp_client:
        bedrock_model = BedrockModel(model_id=resolved_model_id, temperature=0.1)
        agent = Agent(
            model=bedrock_model,
            tools=[mcp_client],
            system_prompt=SYSTEM_PROMPT,
            hooks=[budget],
        )
        prompt = f"Investigate incident {incident_id!r}."
        try:
            result = agent(prompt, structured_output_model=DiagnosisOut)
            diagnosis = result.structured_output
        except Exception as first_exc:
            try:
                repair_prompt = (
                    f"Your previous response did not validate against the required schema "
                    f"({first_exc}). Re-investigate incident {incident_id!r} and return only the "
                    "required structured schema, citing evidence_ids you actually retrieved."
                )
                result = agent(repair_prompt, structured_output_model=DiagnosisOut)
                diagnosis = result.structured_output
            except Exception as second_exc:
                raise InvestigatorError(
                    f"structured output failed after one repair attempt: {second_exc}"
                ) from second_exc

        if diagnosis.incident_id != incident_id:
            raise InvestigatorError(
                f"model returned incident_id {diagnosis.incident_id!r}, expected {incident_id!r}"
            )

    input_tokens, output_tokens = _extract_usage(result)
    return InvestigationResult(
        diagnosis=diagnosis,
        tool_call_count=budget.tool_call_count,
        duration_ms=budget.elapsed_ms,
        model_id=resolved_model_id,
        prompt_version=PROMPT_VERSION,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
