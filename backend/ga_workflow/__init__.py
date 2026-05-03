"""
ga_workflow — TerraScope GA Release Workflow package.

Seven-stage automated pipeline:
  Stage 1+2: ga_detector     — detect latest GA version + analyze changes
  Stage 3+4: ga_implementer  — create branch + generate + apply HCL code
  Stage 5:   ga_validators   — HCL syntax, required attrs, naming, types
  Stage 6:   ga_compat       — provider schema compatibility verification
  Stage 7:   ga_pr_manager   — GitHub PR create / update
  Coord:     ga_orchestrator — runs all stages, manages WorkflowRun state
  Router:    ga_router       — FastAPI endpoints (mount at /api/ga)
"""
from backend.ga_workflow.ga_models import (
    WorkflowRun,
    WorkflowStage,
    GAWorkflowRequest,
    GARelease,
    GAChangeSet,
    GAChange,
    ChangeType,
    BranchResult,
    CodeChange,
    CodeChangeSet,
    ValidationResult,
    ValidatorReport,
    ValidationIssue,
    ValidationSeverity,
    ProviderCompatibility,
    ProviderCompatCheck,
    PRResult,
    PRAction,
    ExistingPR,
    WorkflowLog,
)

__all__ = [
    "WorkflowRun", "WorkflowStage", "GAWorkflowRequest",
    "GARelease", "GAChangeSet", "GAChange", "ChangeType",
    "BranchResult", "CodeChange", "CodeChangeSet",
    "ValidationResult", "ValidatorReport", "ValidationIssue", "ValidationSeverity",
    "ProviderCompatibility", "ProviderCompatCheck",
    "PRResult", "PRAction", "ExistingPR", "WorkflowLog",
]
