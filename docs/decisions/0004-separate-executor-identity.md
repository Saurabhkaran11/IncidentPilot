# 0004. The executor is a separate identity, not a separate function call

**Status:** accepted

## Context

The API knows an approval happened. The obvious shortcut is to have the API
call `UpdateAlias` directly.

## Decision

The API may persist an approval and enqueue an operation. It cannot mutate
the monitored application. A separate executor
(`services/executor/rollback_executor.py`), running as its own worker with
its own AWS identity, consumes the operation ID and re-loads every immutable
parameter from storage.

In live mode these are genuinely different IAM roles: the API/intake role
cannot call `UpdateAlias`; the executor role can update exactly one
registered alias and invoke exactly the registered canary; the investigator
role can do neither and cannot assume the executor role.

## Consequences

A bug or injection in request handling cannot move production traffic. The
cost is a hop: the UI polls an operation instead of getting a synchronous
answer, which is also what makes approvals survive a restart.
