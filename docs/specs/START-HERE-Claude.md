# Start IncidentPilot with Claude

Attach these three files to Claude, or put them in the project repository for Claude Code to read:

1. `IncidentPilot-Claude-Build-Brief.md` — product, design, architecture, AI behavior, permissions, data model, implementation sequence, evaluation, and submission plan.
2. `IncidentPilot-API-Contract.md` — proposed endpoints, payloads, asynchronous jobs, approval rules, errors, and receipt examples.
3. `IncidentPilot-MCP-Tools.json` — five proposed evidence tools with strict input/output JSON Schemas.

Then paste this message:

```text
Build IncidentPilot using the three attached specifications. Start writing the code, product UI, tests, infrastructure, and documentation now. I am targeting Applied AI Engineering and Cloud Engineering roles and entering the Agents for Humans hackathon.

The project helps a small engineering team investigate a failed AWS request-processing workflow, approve a bounded Lambda alias rollback, and verify recovery. Its central product artifact is a recovery receipt that accounts for affected customer requests.

Use Strands Agents SDK for the investigator and AWS services where they serve the working product. Prioritize one complete real-AWS demonstration. Keep cloud mutation in a separate deterministic executor, enforce approvals server-side, and distinguish fixture data from live results. Do not claim requests recovered unless their results were verified.

Read the specifications in full, inspect my repository and tools, and begin the P0 vertical slice. Make routine reversible implementation choices yourself and record consequential tradeoffs. Ask only for information that blocks the next step. If credentials or deployment access are missing, continue building the fixture version and deployable infrastructure, then identify the exact missing setup without asking for secrets in chat.

Check the current time against the September 14, 2026, 5 p.m. Pacific deadline. Protect time for the demo, public repository, architecture diagram, and submission. If this is after the deadline, continue as a portfolio project and do not claim it can still enter the closed event.

Maintain docs/BUILD_STATUS.md with what is implemented, tested, simulated, and deferred. Generate OpenAPI from the implemented API and validate it against the supplied contract. Explain technical decisions as we go so I can defend the architecture and code in interviews. Do not stop after proposing a plan.
```

These files are an implementation handoff. They do not include a built application or evidence of live deployment. All sample IDs, costs, timings, and outcomes in API examples are illustrative.
