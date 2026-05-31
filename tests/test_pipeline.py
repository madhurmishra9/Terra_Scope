"""
tests/test_pipeline.py — Unit tests for the typed sequential pipeline models.

These tests assert that each stage's output validates against its Pydantic model
without requiring a live LLM (all LLM calls are mocked at the function level).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.pipeline.models import (
    QuestionSet,
    GenerationPassResult,
    CurationPipelineOutput,
)


# ── QuestionSet ───────────────────────────────────────────────────────────────

def test_question_set_valid():
    qs = QuestionSet(questions=["What region?", "Which VPC?"])
    assert len(qs.questions) == 2


def test_question_set_empty():
    qs = QuestionSet(questions=[])
    assert qs.questions == []


def test_question_set_round_trip():
    qs = QuestionSet(questions=["Q1?", "Q2?", "Q3?"])
    data = qs.model_dump()
    restored = QuestionSet.model_validate(data)
    assert restored.questions == qs.questions


def test_question_set_json_round_trip():
    qs = QuestionSet(questions=["What project?", "Which region?"])
    json_str = qs.model_dump_json()
    restored = QuestionSet.model_validate_json(json_str)
    assert restored.questions == qs.questions


def test_question_set_requires_list():
    with pytest.raises((ValidationError, TypeError)):
        QuestionSet(questions="not-a-list")  # type: ignore[arg-type]


# ── GenerationPassResult ──────────────────────────────────────────────────────

def test_pass_result_pass_a():
    raw = "[FILE: main.tf]\nresource \"null_resource\" \"x\" {}\n[/FILE]"
    result = GenerationPassResult(
        pass_name="A",
        raw=raw,
        files={"main.tf": 'resource "null_resource" "x" {}'},
        fallback_used=False,
    )
    assert result.pass_name == "A"
    assert "main.tf" in result.files
    assert not result.fallback_used


def test_pass_result_fallback():
    result = GenerationPassResult(
        pass_name="B",
        raw="",
        files={"variables.tf": 'variable "name" { type = string }'},
        fallback_used=True,
    )
    assert result.fallback_used
    assert result.files["variables.tf"].startswith('variable "name"')


def test_pass_result_defaults():
    result = GenerationPassResult(pass_name="C", raw="some raw text")
    assert result.files == {}
    assert result.fallback_used is False


def test_pass_result_round_trip():
    result = GenerationPassResult(
        pass_name="A",
        raw="raw_text",
        files={"main.tf": "# content"},
        fallback_used=False,
    )
    restored = GenerationPassResult.model_validate(result.model_dump())
    assert restored.pass_name == result.pass_name
    assert restored.files == result.files


# ── CurationPipelineOutput ────────────────────────────────────────────────────

def test_pipeline_output_empty():
    out = CurationPipelineOutput()
    assert out.questions == []
    assert out.pass_a is None
    assert out.all_files == {}
    assert out.error is None


def test_pipeline_output_with_error():
    out = CurationPipelineOutput(error="Pass A failed: connection refused")
    assert out.error is not None
    assert "Pass A" in out.error


def test_pipeline_output_all_passes():
    pa = GenerationPassResult(pass_name="A", raw="", files={"main.tf": "# main"})
    pb = GenerationPassResult(pass_name="B", raw="", files={"variables.tf": "# vars"})
    pc = GenerationPassResult(pass_name="C", raw="", files={"versions.tf": "# ver"})
    out = CurationPipelineOutput(
        questions=["Q1?", "Q2?"],
        pass_a=pa,
        pass_b=pb,
        pass_c=pc,
        all_files={"main.tf": "# main", "variables.tf": "# vars", "versions.tf": "# ver"},
    )
    assert len(out.questions) == 2
    assert out.pass_a is not None
    assert len(out.all_files) == 3


def test_pipeline_output_round_trip():
    pa = GenerationPassResult(pass_name="A", raw="r", files={"main.tf": "x"})
    out = CurationPipelineOutput(questions=["Q?"], pass_a=pa, all_files={"main.tf": "x"})
    restored = CurationPipelineOutput.model_validate(out.model_dump())
    assert restored.questions == ["Q?"]
    assert restored.pass_a is not None
    assert restored.pass_a.pass_name == "A"
