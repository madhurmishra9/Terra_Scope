"""
troubleshooter/models.py — Pydantic models for the Terraform module troubleshooter.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional
from pydantic import BaseModel


class IssueSeverity(str, Enum):
    ERROR   = "error"
    WARNING = "warning"
    INFO    = "info"


class IssueCategory(str, Enum):
    SYNTAX       = "syntax"
    TYPE         = "type"
    UNDEFINED    = "undefined_ref"
    MISSING_ATTR = "missing_attr"
    DEPRECATED   = "deprecated"
    SECURITY     = "security"
    LOGIC        = "logic"
    BEST_PRACTICE = "best_practice"
    PROVIDER     = "provider"


class TroubleshootIssue(BaseModel):
    severity:     IssueSeverity
    category:     IssueCategory
    file_path:    str = ""
    line:         Optional[int] = None
    resource_type: str = ""
    resource_name: str = ""
    message:      str
    suggestion:   str = ""
    fixed_in_version: Optional[str] = None   # provider version that fixed this issue


class VersionRecommendation(BaseModel):
    current_version:     str
    recommended_version: str
    reason:              str
    breaking_changes:    int = 0
    safe_to_upgrade:     bool
    changelog_url:       str = ""
    fixes_in_version:    list[str] = []      # human-readable list of what's fixed


class TroubleshootResult(BaseModel):
    repo_name:   str
    tag:         str
    scan_date:   str
    issues:      list[TroubleshootIssue]
    error_count:   int = 0
    warning_count: int = 0
    info_count:    int = 0
    version_recommendation: Optional[VersionRecommendation] = None
    summary:     str
    scanned_files: list[str] = []
