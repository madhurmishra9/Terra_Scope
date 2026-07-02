"""
Typed output models for each stage of the curation pipeline.
Every Agent in the pipeline declares output_type=<one of these models>.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class CurationOutcome(str, Enum):
    """Escalation as a first-class outcome (Priority 3): distinguishes a
    module that is genuinely ready for curator approval from one that only
    *looks* shippable because it survived the repair loop's round cap."""
    CANDIDATE_READY = "candidate_ready"   # all gates passed -> curator may approve
    ESCALATED = "escalated"               # repair budget exhausted -> human required
    FAILED = "failed"                     # a pass itself errored (existing .error path)


class QuestionSet(BaseModel):
    """Output of the question-generation stage."""
    questions: list[str] = Field(
        description="Ordered list of clarifying questions to ask the user."
    )


class GenerationPassResult(BaseModel):
    """Typed wrapper returned by each of the three code-generation passes.

    The raw LLM text is preserved in ``raw`` so the existing [FILE:] marker
    parser can still extract individual files.  ``files`` holds the already-
    parsed mapping (filename → HCL content) after extraction succeeds.
    """
    pass_name: str = Field(description="A / B / C identifying which pass produced this result.")
    raw: str = Field(description="Raw LLM output including [FILE:] markers.")
    files: dict[str, str] = Field(
        default_factory=dict,
        description="Parsed files extracted from raw output (filename → HCL content).",
    )
    fallback_used: bool = Field(
        default=False,
        description="True when no [FILE:] markers were found and stub fallbacks were injected.",
    )


class CurationPipelineOutput(BaseModel):
    """Final aggregate output of the full curation pipeline."""
    questions: list[str] = Field(
        default_factory=list,
        description="All questions (initial + follow-ups) posed to the user.",
    )
    pass_a: Optional[GenerationPassResult] = Field(
        default=None, description="main.tf generation result."
    )
    pass_b: Optional[GenerationPassResult] = Field(
        default=None, description="variables.tf + outputs.tf generation result."
    )
    pass_c: Optional[GenerationPassResult] = Field(
        default=None, description="versions.tf + README + examples generation result."
    )
    all_files: dict[str, str] = Field(
        default_factory=dict,
        description="Merged file map from all three passes (filename → HCL content).",
    )
    error: Optional[str] = Field(
        default=None, description="Set if any stage failed fatally."
    )
    outcome: Optional[CurationOutcome] = Field(
        default=None, description="Escalation-aware result of the pipeline run.",
    )
    outstanding_issues: list[str] = Field(
        default_factory=list,
        description="Unresolved validator errors when outcome=ESCALATED.",
    )
