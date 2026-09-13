"""Shared domain layer: entities, state machine, and plan canonicalization.

Every service (api, worker, executor, agent, mcp_server) imports from here so
that "what a recovered incident means" is defined exactly once.
"""
