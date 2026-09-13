"""Enforce the tool-call and wall-clock budget in code, not in the prompt.

Build brief section 8: "Suggested limits: 12 tool calls, 90 seconds per
investigation ... Enforce limits in code; do not rely on the prompt." Strands
does not ship a combined call-count + wall-clock budget, so this is a small
``HookProvider`` that cancels further tool calls once either limit is hit.
"""

from __future__ import annotations

import time

from strands.hooks import BeforeInvocationEvent, BeforeToolCallEvent, HookProvider, HookRegistry


class InvestigationBudgetHook(HookProvider):
    def __init__(self, max_tool_calls: int, max_duration_seconds: float) -> None:
        self.max_tool_calls = max_tool_calls
        self.max_duration_seconds = max_duration_seconds
        self.tool_call_count = 0
        self._started: float | None = None

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(BeforeInvocationEvent, self._on_start)
        registry.add_callback(BeforeToolCallEvent, self._on_before_tool)

    def _on_start(self, event: BeforeInvocationEvent) -> None:
        self._started = time.monotonic()
        self.tool_call_count = 0

    def _on_before_tool(self, event: BeforeToolCallEvent) -> None:
        elapsed = time.monotonic() - (self._started or time.monotonic())
        if self.tool_call_count >= self.max_tool_calls:
            event.cancel_tool = f"tool-call budget of {self.max_tool_calls} exceeded"
            return
        if elapsed > self.max_duration_seconds:
            event.cancel_tool = f"investigation time budget of {self.max_duration_seconds}s exceeded"
            return
        self.tool_call_count += 1

    @property
    def elapsed_ms(self) -> int:
        if self._started is None:
            return 0
        return int((time.monotonic() - self._started) * 1000)
