"""
backend/pipeline — Typed sequential PydanticAI pipeline for curation.

Stages (in order):
  1. QuestionStage   → QuestionSet
  2. FollowupStage   → QuestionSet (0-3 follow-up questions)
  3. PassAStage      → GenerationPassResult (main.tf)
  4. PassBStage      → GenerationPassResult (variables.tf + outputs.tf)
  5. PassCStage      → GenerationPassResult (versions.tf + README + examples)

Each stage is a PydanticAI Agent with output_type=<TypedModel>.
The CurationPipeline runner threads typed outputs forward in order.
"""
from backend.pipeline.models import QuestionSet, GenerationPassResult, CurationPipelineOutput
from backend.pipeline.runner import CurationPipeline

__all__ = ["QuestionSet", "GenerationPassResult", "CurationPipelineOutput", "CurationPipeline"]
