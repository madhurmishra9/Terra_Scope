"""
models.py — All Pydantic models for agent I/O.
Strict typing ensures no hallucinated fields propagate to the UI.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class QueryType(str, Enum):
    GENERAL     = "general"      # What does this module do?
    ISSUE       = "issue"        # Why does plan/apply fail?
    COMPARISON  = "comparison"   # What changed between v1 and v2?
    VARIABLE    = "variable"     # What variables are required?
    RESOURCE    = "resource"     # What GCP resources are created?
    SECURITY    = "security"     # IAM roles, permissions, CMEK?
    DEPENDENCY  = "dependency"   # Provider versions, required APIs?
    UNKNOWN     = "unknown"      # Cannot classify


class SourceReference(BaseModel):
    """A precise citation: file, tag, lines, and the actual code snippet."""
    repo_name:   str
    file_path:   str
    tag:         str
    line_start:  int
    line_end:    int
    snippet:     str = Field(max_length=2000)
    relevance:   float = Field(ge=0.0, le=1.0, default=0.0)


class IssueSolution(BaseModel):
    """Structured remediation for a detected issue."""
    error_pattern:          str
    root_cause:             str
    affected_resource_type: Optional[str] = None
    solution_steps:         list[str]
    gcloud_commands:        list[str] = []
    related_files:          list[str] = []
    terraform_fix:          Optional[str] = None   # HCL snippet fix if applicable
    provider_version_note:  Optional[str] = None


class VariableInfo(BaseModel):
    name:        str
    type:        str
    description: str
    default:     Optional[str] = None
    required:    bool
    file_path:   str
    line:        int


class ResourceInfo(BaseModel):
    resource_type:  str   # e.g. google_bigquery_dataset
    resource_name:  str   # logical name in HCL
    file_path:      str
    line_start:     int
    attributes:     dict  # key HCL attributes extracted


class AgentResponse(BaseModel):
    """
    The fully-typed response returned by the PydanticAI agent.
    Every field has a strict contract — no free-form dicts.
    """
    query_type:       QueryType
    answer:           str                    = Field(description="Direct, fact-based answer")
    confidence:       float                  = Field(ge=0.0, le=1.0)
    grounded:         bool                   = Field(description="True if answer is backed by repo code")
    sources:          list[SourceReference]  = []
    issue_solution:   Optional[IssueSolution] = None
    variables:        list[VariableInfo]     = []
    resources:        list[ResourceInfo]     = []
    tags_analyzed:    list[str]              = []
    repo_name:        str                    = ""
    disclaimer:       Optional[str]          = None  # shown when confidence < threshold


# ── Request models ─────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    question:    str      = Field(min_length=3, max_length=2000)
    repo_name:   Optional[str] = None   # None = search all enabled repos
    tag:         Optional[str] = None   # None = latest tag
    strict_mode: bool = True            # Enforce grounding (override per-query)


class IndexRequest(BaseModel):
    repo_name: Optional[str] = None   # None = index all enabled repos
    force:     bool = False           # Force re-index even if already indexed


class IndexStatus(BaseModel):
    repo_name:       str
    display_name:    str
    gcp_product:     str
    tags_indexed:    list[str]
    total_chunks:    int
    last_indexed_at: Optional[str]
    status:          str   # ready | indexing | error | not_found
    error:           Optional[str] = None
