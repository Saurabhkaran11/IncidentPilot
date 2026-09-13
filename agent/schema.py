"""The agent's structured output contract (build brief section 8).

This is deliberately narrower than ``packages.domain.models.Diagnosis``: the
model may only produce these fields. Everything else on ``Diagnosis``
(``diagnosis_id``, ``agent_mode``, ``model_id``, ``tool_call_count``,
``duration_ms``, ``created_at``) is filled in by ``agent/investigator.py``
from the *host process*, never asked of the model -- so a prompt-injected
log line cannot forge a model ID or a timestamp.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from packages.domain.enums import Assessment, RecommendedAction


class HypothesisOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(description="Plain-language candidate cause.")
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)


class DiagnosisOut(BaseModel):
    """Exact shape requested via Strands ``structured_output_model``."""

    model_config = ConfigDict(extra="forbid")

    incident_id: str
    assessment: Assessment
    summary: str = Field(description="Short operator-facing explanation, not chain-of-thought.")
    hypotheses: list[HypothesisOut] = Field(default_factory=list)
    evidence_ids: list[str] = Field(
        default_factory=list, description="Must cite evidence_ids returned by tool calls in this run."
    )
    unknowns: list[str] = Field(default_factory=list)
    recommended_action: RecommendedAction
