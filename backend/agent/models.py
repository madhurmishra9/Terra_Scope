"""
models.py — All Pydantic models for agent I/O.
Strict typing ensures no hallucinated fields propagate to the UI.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


# ── Query / Analysis models ────────────────────────────────────────────────────

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
    terraform_fix:          Optional[str] = None
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
    resource_type:  str
    resource_name:  str
    file_path:      str
    line_start:     int
    attributes:     dict


class AgentResponse(BaseModel):
    """
    The fully-typed response returned by the query/analysis agent.
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
    disclaimer:       Optional[str]          = None


# ── Generation models ──────────────────────────────────────────────────────────

class GenerationMode(str, Enum):
    EXTEND  = "extend"   # Add features/fixes to an existing module
    NEW     = "new"      # Create a net-new module from scratch
    COMPOSE = "compose"  # Composite module wiring existing modules together


class GeneratedFile(BaseModel):
    """A single generated or diff'd file."""
    path:        str            # e.g. "repos/terraform-google-pubsub/main.tf"
    content:     str            # Full file content or unified diff
    is_diff:     bool  = False  # True when content is a unified diff
    description: str   = ""     # What this file does / what changed


class ValidationNote(BaseModel):
    """A single validation finding from the post-generation checks."""
    level:   str            # security | cost | lint | info | error
    file:    str            # relative file path (or "" for module-level)
    message: str
    line:    Optional[int] = None


class GenerateRequest(BaseModel):
    """Request to the HCL generation pipeline."""
    mode:                     GenerationMode
    target_module:            str   = Field(
        description="Short module name, e.g. 'terraform-google-pubsub'",
        min_length=3,
    )
    intent:                   str   = Field(
        description="Natural-language description of what to generate or change",
        min_length=10,
        max_length=5000,
    )
    gcp_product:              Optional[str]  = None   # bigquery | storage | pubsub | ...
    base_repos:               list[str]      = []     # existing repos to draw context from
    provider_version:         str            = ">= 5.39, < 8"
    enable_security_baseline: bool           = True


class GenerationResponse(BaseModel):
    """Complete output from the HCL generation pipeline."""
    mode:             GenerationMode
    target_module:    str
    files:            list[GeneratedFile]
    validation_notes: list[ValidationNote]
    resource_count:   int          = 0
    variable_count:   int          = 0
    ready:            bool         = False   # True when no error-level notes
    disclaimer:       Optional[str] = None


# ── Request models ─────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    question:    str      = Field(min_length=3, max_length=2000)
    repo_name:   Optional[str] = None
    tag:         Optional[str] = None
    strict_mode: bool = True


class IndexRequest(BaseModel):
    repo_name: Optional[str] = None
    force:     bool = False


class IndexStatus(BaseModel):
    repo_name:       str
    display_name:    str
    gcp_product:     str
    tags_indexed:    list[str]
    total_chunks:    int
    last_indexed_at: Optional[str]
    status:          str
    error:           Optional[str] = None
