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

Multi-cloud support (v2.3):
  - CloudProvider enum covers Google (GCP), AWS, Azure
  - BreakingReason enum classifies WHY a change is breaking
  - GAChange carries cloud_provider + breaking_reason + migration_note
  - IncrementalState tracks applied changes per repo to avoid re-work
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────────────────────

class CloudProvider(str, Enum):
    GCP   = "google"    # hashicorp/google
    AWS   = "aws"       # hashicorp/aws
    AZURE = "azurerm"   # hashicorp/azurerm


class WorkflowStage(str, Enum):
    IDLE             = "idle"
    DETECTING        = "detecting_ga"
    SCANNING_SERVICE = "scanning_gcp_service"
    ANALYZING        = "analyzing_changes"
    BRANCHING        = "creating_branch"
    IMPLEMENTING     = "implementing_changes"
    VALIDATING       = "validating_code"
    CHECKING_PR      = "checking_pr"
    CREATING_PR      = "creating_pr"
    UPDATING_PR      = "updating_pr"
    DONE             = "done"
    FAILED           = "failed"


class ChangeType(str, Enum):
    NEW_RESOURCE       = "new_resource"
    NEW_ARGUMENT       = "new_argument"
    DEPRECATED_ARG     = "deprecated_argument"
    NEW_VARIABLE       = "new_variable"
    UPDATED_VARIABLE   = "updated_variable"
    REMOVED_VARIABLE   = "removed_variable"
    PROVIDER_VERSION   = "provider_version"
    NEW_OUTPUT         = "new_output"
    IAM_CHANGE         = "iam_change"
    API_REQUIREMENT    = "api_requirement"
    VALIDATION_RULE    = "validation_rule"
    LIFECYCLE_CHANGE   = "lifecycle_change"


class BreakingReason(str, Enum):
    """WHY a change is breaking — drives migration guidance shown to the user."""
    REMOVED            = "removed"            # resource or argument no longer exists
    RENAMED            = "renamed"            # identifier changed; old name rejected
    TYPE_CHANGED       = "type_changed"       # e.g. string → list, number → bool
    REQUIRED_NOW       = "required_now"       # was optional, now required with no default
    BEHAVIOR_CHANGED   = "behavior_changed"   # default value or validation logic changed
    DEPRECATED_REMOVED = "deprecated_removed" # was deprecated, now fully removed
    UNKNOWN            = "unknown"            # breaking signal found but reason unclear


class ValidationSeverity(str, Enum):
    ERROR   = "error"
    WARNING = "warning"
    INFO    = "info"


class PRAction(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    SKIPPED = "skipped"
    FAILED  = "failed"


# ── Stage 1: GA Release Detection ─────────────────────────────────────────────

class GAChange(BaseModel):
    """One discrete GA change from the provider changelog."""
    change_type:      ChangeType
    resource_type:    str                              # e.g. google_bigquery_dataset / aws_s3_bucket
    attribute_name:   Optional[str] = None            # e.g. "max_time_travel_hours"
    description:      str                             # Human-readable description of the change
    provider_version: str                             # First provider version that includes this
    breaking:         bool = False                    # True if this is a breaking change
    breaking_reason:  Optional[BreakingReason] = None # WHY it is breaking
    migration_note:   Optional[str] = None            # Plain-English migration guidance
    migration_guide:  Optional[str] = None            # HCL snippet showing how to migrate
    cloud_provider:   CloudProvider = CloudProvider.GCP
    source_url:       Optional[str] = None


class GARelease(BaseModel):
    """Metadata about the latest GA provider release."""
    provider:            str = "hashicorp/google"
    cloud_provider:      CloudProvider = CloudProvider.GCP
    current_version:     str
    latest_ga_version:   str
    upgrade_required:    bool
    breaking_changes:    int = 0
    new_features:        int = 0
    changelog_url:       str = ""
    fetched_at:          str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class GAChangeSet(BaseModel):
    """Full analysis: what changed between current and latest GA provider."""
    repo_name:        str
    gcp_product:      str            # generic cloud product/service label
    current_tag:      str
    ga_release:       GARelease
    changes:          list[GAChange]
    new_changes:      list[GAChange] = []  # subset not yet applied (incremental)
    files_to_modify:  list[str]
    summary:          str


# ── Incremental state ─────────────────────────────────────────────────────────

class IncrementalState(BaseModel):
    """
    Persisted per-repo state that lets the GA workflow skip changes
    that have already been implemented in a previous run.
    Stored at ./data/ga_state/{repo_name}.json
    """
    repo_name:               str
    last_scan:               str = ""
    applied_provider_version: str = ""
    applied_change_hashes:   list[str] = []
    service_features_seen:   list[str] = []


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
    attribute_name:  Optional[str] = None
    supported:       bool
    min_version:     Optional[str] = None  # Minimum provider version needed
    deprecated_in:   Optional[str] = None  # Provider version where it was deprecated
    notes:           Optional[str] = None


class ProviderCompatibility(BaseModel):
    """Full provider compatibility report for all proposed changes."""
    repo_name:         str
    current_version:   str
    target_version:    str
    all_compatible:    bool
    checks:            list[ProviderCompatCheck]
    versions_tf_update: Optional[str] = None  # New versions.tf content if bump needed


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
    gcp_service_scan:    Optional["GCPServiceScanResult"] = None
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


# ── GCP Service Scan models ───────────────────────────────────────────────────

class GCPServiceFeatureModel(BaseModel):
    feature_name:        str
    description:         str
    announced_date:      str = ""
    product:             str = ""
    source:              str = ""
    terraform_impact:    str = "unknown"
    terraform_resources: list[str] = []
    terraform_args:      list[str] = []
    source_url:          str = ""
    ga_confirmed:        bool = False

    def to_dict(self) -> dict:
        return self.model_dump()


class GCPServiceScanResult(BaseModel):
    repo_name:           str
    gcp_product:         str
    scan_date:           str
    total_features:      int = 0
    actionable_count:    int = 0
    features:            list[GCPServiceFeatureModel] = []
    actionable_features: list[GCPServiceFeatureModel] = []
    module_resources:    list[str] = []
    summary:             str = ""

    def to_dict(self) -> dict:
        return self.model_dump()


# ── Request / Response for API ────────────────────────────────────────────────

class GAWorkflowRequest(BaseModel):
    repo_name:     str
    base_branch:   str = "main"        # Branch to base the GA branch off
    github_token:  Optional[str] = None  # If None, read from GITHUB_TOKEN env var
    pr_labels:     list[str] = ["ga-release", "automated", "terraform"]
    dry_run:       bool = False        # If True, don't push or create PR
    auto_fix:      bool = True         # Auto-fix fixable validation issues
