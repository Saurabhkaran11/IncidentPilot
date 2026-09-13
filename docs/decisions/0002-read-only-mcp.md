# 0002. The agent's tools are read-only, and that is enforced structurally

**Status:** accepted

## Context

MCP tool annotations include `readOnlyHint`. That is a *hint* -- a label in
a JSON document, not an enforcement mechanism. Anything that relied on it
would be relying on the model's good manners.

## Decision

There is no mutating tool. The five tools in
`contracts/mcp/evidence-tools.json` only read. Enforcement is structural, at
three layers:

1. `mcp_server/server.py` never imports `services/executor`. There is no
   code path from the agent process to `UpdateAlias`.
2. The server process is bound to one `incident_id` at spawn
   (`mcp_server/context.py`). A tool call naming any other incident returns
   `RESOURCE_OUT_OF_SCOPE` regardless of what the model asks for.
3. In live mode the investigator's AWS session carries a read-only role.
   Sharing a repository is not sharing an IAM role.

## Consequences

The worst outcome of a fully compromised or hallucinating agent is a wrong
*recommendation*, which a human then declines. Prompt injection in a log
line (tested in `tests/integration/test_prompt_injection.py`) cannot acquire
authority that no tool exposes.
