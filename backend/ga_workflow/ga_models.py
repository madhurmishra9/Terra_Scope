"""
ga_models.py — Typed Pydantic models for the entire GA Release Workflow.

Pipeline stages:
  1. GA Release Detection   → GARelease, GAChangeSet
  2. Branch Management      → BranchResult
  3. Code Generation        → CodeChange, CodeChangeSet
  4. Validation             → ValidationResult, ValidatorReport
  5. Provider Compatibility → ProviderCompatibility
  6. PR Management          → PRStatus, PRResult
  7. Orchestrator           → WorkflowRun (full pipeline state)
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────────────────────

class WorkflowStage(str, Enum):
    IDLE            = "idle"
    DETECTING       = "detecting_ga"
    ANALYZING       = "analyzing_changes"
    BRANCHING       = "creating_branch"
    IMPLEMENTING    = "implementing_changes"
    VALIDATING      = "validating_code"
    CHECKING_PR     = "checking_pr"
    CREATING_PR     = "creating_pr"
    UPDATING_PR     = "updating_pr"
    DONE            = "done"
    FAILED          = "failed"


class ChangeType(str, Enum):
    NEW_RESOURCE       = "new_resource"         # Brand-new GCP resource type
    NEW_ARGUMENT       = "new_argument"         # New field on existing resource
    DEPRECATED_ARG     = "deprecated_argument"  # Field removed/deprecated
    NEW_VARIABLE       = "new_variable"         # New module input variable
    UPDATED_VARIABLE   = "updated_variable"     # Changed type/default/desc
    REMOVED_VARIABLE   = "removed_variable"     # Variable dropped
    PROVIDER_VERSION   = "provider_version"     # Required provider version bump
    NEW_OUTPUT         = "new_output"           # New module output
    IAM_CHANGE         = "iam_change"           # New IAM role or binding pattern
    API_REQUIREMENT    = "api_requirement"      # New GCP API enablement required
    VALIDATION_RULE    = "validation_rule"      # New validation block on variable
    LIFECYCLE_CHANGE   = "lifecycle_change"     # lifecycle{} block change


class ValidationSeverity(str, Enum):
    ERROR   = "error"    # Must fix before PR
    WARNING = "warning"  # Should fix, not blocking
    INFO    = "info"     # Informational only


class PRAction(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    SKIPPED = "skipped"   # Already up-to-date
    FAILED  = "failed"


# ── Stage 1: GA Release Detection ─────────────────────────────────────────────

class GAChange(BaseModel):
    """One discrete GA change from the provider changelog."""
    change_type:      ChangeType
    resource_type:    str              # e.g. google_bigquery_dataset
    attribute_name:   Optional[str]   # e.g. "max_time_travel_hours"
    description:      str             # Human-readable description of the change
    provider_version: str             # First provider version that includes this
    breaking:         bool = False    # True if this is a breaking change
    migration_guide:  Optional[str]  # HCL migration snippet if breaking
    source_url:       Optional[str]  # Link to provider changelog/docs


class GARelease(BaseModel):
    """Metadata about the latest GA provider release."""
    provider:            str = "hashicorp/google"
    current_version:     str        # Version currently used in the module
    latest_ga_version:   str        # Latest available GA version
    upgrade_required:    bool       # True if latest > current
    breaking_changes:    int = 0    # Count of breaking changes
    new_features:        int = 0    # Count of new features
    changelog_url:       str = ""
    fetched_at:          str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class GAChangeSet(BaseModel):
    """Full analysis: what changed between current and latest GA provider."""
    repo_name:        str
    gcp_product:      str
    current_tag:      str           # Module tag currently analyzed
    ga_release:       GARelease
    changes:          list[GAChange]
    files_to_modify:  list[str]     # .tf files that need editing
    summary:          str           # One-paragraph human summary


# ── Stage 2: Branch Management ────────────────────────────────────────────────

class BranchResult(BaseModel):
    repo_name:    str
    branch_name:  str
    base_branch:  str
    created:      bool
    already_existed: bool = False
    error:        Optional[str] = None


# ── Stage 3: Code Changes ─────────────────────────────────────────────────────

class CodeChange(BaseModel):
    """One atomic edit to a single file."""
    file_path:    str
    change_type:  ChangeType
    description:  str
    old_content:  Optional[str] = None   # The block being replaced (None = addition)
    new_content:  str                    # The replacement / addition
    line_hint:    Optional[int] = None   # Approximate target line for insertion
    ga_change:    Optional[GAChange] = None  # Back-reference to the GA change


class CodeChangeSet(BaseModel):
    """All code edits to implement the GA changes."""
    repo_name:      str
    branch_name:    str
    changes:        list[CodeChange]
    changelog_entry: str              # Text to prepend to CHANGELOG.md
    commit_message: str               # Git commit message
    applied:        bool = False
    apply_errors:   list[str] = []


# ── Stage 4: Validation ───────────────────────────────────────────────────────

class ValidationIssue(BaseModel):
    """One validation finding."""
    severity:    ValidationSeverity
    file_path:   str
    line:        Optional[int] = None
    rule:        str             # Rule name: "hcl_syntax", "required_attr", etc.
    message:     str
    suggestion:  Optional[str] = None  # How to fix it


class ValidatorReport(BaseModel):
    """Result from one validator."""
    validator_name:  str
    passed:          bool
    issues:          list[ValidationIssue] = []
    duration_ms:     int = 0


class ValidationResult(BaseModel):
    """Aggregated result from all validators."""
    repo_name:       str
    branch_name:     str
    overall_passed:  bool
    error_count:     int
    warning_count:   int
    reports:         list[ValidatorReport]
    validated_files: list[str]
    validated_at:    str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# ── Stage 5: Provider Compatibility ──────────────────────────────────────────

class ProviderCompatCheck(BaseModel):
    """Result of checking one resource/attribute against the provider schema."""
    resource_type:   str
    attribute_name:  Optional[str]
    supported:       bool
    min_version:     Optional[str]   # Minimum provider version needed
    deprecated_in:   Optional[str]   # Provider version where it was deprecated
    notes:           Optional[str]


class ProviderCompatibility(BaseModel):
    """Full provider compatibility report for all proposed changes."""
    repo_name:         str
    current_version:   str
    target_version:    str
    all_compatible:    bool
    checks:            list[ProviderCompatCheck]
    versions_tf_update: Optional[str]  # New versions.tf content if bump needed


# ── Stage 6: PR Management ────────────────────────────────────────────────────

class ExistingPR(BaseModel):
    """A PR already open in the repo."""
    number:      int
    title:       str
    url:         str
    branch:      str
    state:       str        # open | closed | merged
    created_at:  str
    updated_at:  str
    body:        str = ""


class PRResult(BaseModel):
    """Outcome of the PR create/update operation."""
    action:       PRAction
    pr_number:    Optional[int]  = None
    pr_url:       Optional[str]  = None
    pr_title:     str            = ""
    pr_body:      str            = ""
    branch:       str            = ""
    target_branch: str           = "main"
    existing_pr:  Optional[ExistingPR] = None
    error:        Optional[str]  = None


# ── Stage 7: Full Workflow Run ────────────────────────────────────────────────

class WorkflowLog(BaseModel):
    """One log entry in the workflow run."""
    timestamp:  str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    stage:      WorkflowStage
    level:      str = "info"   # info | warning | error
    message:    str
    detail:     Optional[str] = None


class WorkflowRun(BaseModel):
    """
    Complete state of one GA workflow execution.
    Updated incrementally as each stage completes.
    Serialized to JSON and returned to the UI for live progress display.
    """
    run_id:          str
    repo_name:       str
    gcp_product:     str
    started_at:      str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    completed_at:    Optional[str] = None
    stage:           WorkflowStage = WorkflowStage.IDLE
    overall_success: bool = False

    # Stage outputs — populated as each stage completes
    ga_release:          Optional[GARelease]          = None
    change_set:          Optional[GAChangeSet]         = None
    branch_result:       Optional[BranchResult]        = None
    code_changes:        Optional[CodeChangeSet]       = None
    validation_result:   Optional[ValidationResult]    = None
    provider_compat:     Optional[ProviderCompatibility] = None
    pr_result:           Optional[PRResult]            = None

    logs:            list[WorkflowLog] = []
    error:           Optional[str]     = None

    def log(self, message: str, level: str = "info", detail: Optional[str] = None):
        self.logs.append(WorkflowLog(
            stage=self.stage,
            level=level,
            message=message,
            detail=detail,
        ))

    def fail(self, message: str, detail: Optional[str] = None):
        self.log(message, level="error", detail=detail)
        self.stage = WorkflowStage.FAILED
        self.error = message
        self.completed_at = datetime.utcnow().isoformat()


# ── Request / Response for API ────────────────────────────────────────────────

class GAWorkflowRequest(BaseModel):
    repo_name:     str
    base_branch:   str = "main"        # Branch to base the GA branch off
    github_token:  Optional[str] = None  # If None, read from GITHUB_TOKEN env var
    pr_labels:     list[str] = ["ga-release", "automated", "terraform"]
    dry_run:       bool = False        # If True, don't push or create PR
    auto_fix:      bool = True         # Auto-fix fixable validation issues
