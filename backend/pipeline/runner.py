"""
CurationPipeline — thin sequential runner that chains curation stages in order.

Each stage is independently testable; the runner just threads typed outputs
forward.  The actual LLM calls live in question_engine and code_generator;
this file only orchestrates calling order and aggregation.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from backend.pipeline.models import CurationPipelineOutput, GenerationPassResult

if TYPE_CHECKING:
    # Type-only import — avoids a circular import at module load time since
    # module_curator.models now imports CurationOutcome from backend.pipeline.models.
    from backend.module_curator.models import CurationSession


class CurationPipeline:
    """Sequential pipeline runner for the curation workflow.

    Usage::

        pipeline = CurationPipeline(session)
        output = await pipeline.run_generation()   # runs passes A → B → C
    """

    def __init__(self, session: CurationSession) -> None:
        self._session = session

    async def run_generation(self) -> CurationPipelineOutput:
        """Run generation passes A → B → C; aggregate into CurationPipelineOutput."""
        from backend.module_curator.code_generator import (
            _run_pass_a, _run_pass_b, _run_pass_c,
        )

        output = CurationPipelineOutput()
        all_files: dict[str, str] = {}

        try:
            pass_a: GenerationPassResult = await _run_pass_a(self._session)
            output.pass_a = pass_a
            all_files.update(pass_a.files)
        except Exception as exc:
            output.error = f"Pass A failed: {exc}"
            return output

        try:
            pass_b: GenerationPassResult = await _run_pass_b(self._session, pass_a.raw)
            output.pass_b = pass_b
            all_files.update(pass_b.files)
        except Exception as exc:
            output.error = f"Pass B failed: {exc}"
            return output

        try:
            pass_c: GenerationPassResult = await _run_pass_c(self._session, all_files)
            output.pass_c = pass_c
            all_files.update(pass_c.files)
        except Exception as exc:
            output.error = f"Pass C failed: {exc}"
            return output

        output.all_files = all_files
        return output
