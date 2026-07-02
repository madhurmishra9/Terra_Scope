"""
models.py — Pydantic models for the Module Curation pipeline.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from backend.pipeline.models import CurationOutcome


class CurationMode(str, Enum):
    NEW_PRODUCT    = "new_product"    # Generate module from service name + registry docs
    FROM_DOCUMENT  = "from_document"  # Generate from uploaded spec (PDF/DOCX/TXT)
    FROM_MODULE    = "from_module"    # Create new module from existing .tf source
    SELF_CURATION  = "self_curation"  # Modify existing indexed repo and create a new Git tag


class CloudProvider(str, Enum):
    GCP   = "google"
    AWS   = "aws"
    AZURE = "azurerm"


class SessionStatus(str, Enum):
    INIT             = "init"
    GATHERING        = "gathering"        # Waiting for source (doc / module / etc.)
    ASKING           = "asking"           # LLM is asking primary clarifying questions
    ASKING_FOLLOWUP  = "asking_followup"  # Engine determined more info is needed after first pass
    READY            = "ready"            # All questions answered, ready to generate
    GENERATING       = "generating"
    DONE             = "done"
    ERROR            = "error"


class QAPair(BaseModel):
    question: str
    answer:   str


class GeneratedFile(BaseModel):
    filename: str
    content:  str


class CurationValidationIssue(BaseModel):
    """One finding from the post-generation validation pipeline."""
    severity:   str            # "error" | "warning" | "info"
    file:       str
    line:       Optional[int] = None
    rule:       str            = ""
    message:    str
    suggestion: str            = ""


class CurationValidationResult(BaseModel):
    """Aggregated result from all four validation layers."""
    passed:                    bool
    error_count:               int            = 0
    warning_count:             int            = 0
    issues:                    list[CurationValidationIssue] = []
    terraform_cli_available:   bool           = False
    terraform_validate_passed: Optional[bool] = None
    provider_schema_checked:   bool           = False


class LocalModuleRef(BaseModel):
    """Lightweight projection of a LocalModule for the SessionView."""
    name:           str
    display_name:   str
    rel_path:       str
    resource_types: list[str] = []
    required_inputs: list[str] = []
    match_reason:   str = ""


class DependentModuleRef(BaseModel):
    """Lightweight projection of a ResolvedDependency for the SessionView."""
    raw_source:      str
    kind:            str            # local | repos | chromadb | registry | unresolved
    depth:           int            = 0
    required_inputs: list[str] = []
    outputs:         list[str] = []


class GenerationResult(BaseModel):
    files:             list[GeneratedFile]
    summary:           str
    usage_example:     str
    output_dir:        str
    git_tag_created:   bool         = False
    git_tag_name:      Optional[str] = None
    validation:        Optional[CurationValidationResult] = None
    local_modules_used: list[LocalModuleRef] = []
    dependent_modules:  list[DependentModuleRef] = []
    outcome:            Optional[CurationOutcome] = None
    outstanding_issues: list[str] = []


class CurationSession(BaseModel):
    session_id:   str
    mode:         CurationMode
    provider:     CloudProvider
    service_name: str = ""

    # Self-curation fields
    repo_name: Optional[str] = None
    new_tag:   Optional[str] = None

    # Gathered context
    document_text:      str              = ""
    tf_files:           dict[str, str]   = {}   # filename → HCL content
    referenced_modules: dict[str, str]   = {}   # source URL/path → content snippet (legacy)
    registry_docs:      str              = ""

    # NEW in v2.1 — local repos integration
    local_modules:     list[LocalModuleRef]     = []
    dependent_modules: list[DependentModuleRef] = []

    # Q&A state
    questions:            list[str]    = []
    current_question_idx: int          = 0
    qa_pairs:             list[QAPair] = []
    followup_round:       int          = 0   # NEW
    max_followup_rounds:  int          = 1   # NEW: cap to prevent infinite loops

    # Result / error
    status: SessionStatus      = SessionStatus.INIT
    error:  Optional[str]      = None
    result: Optional[GenerationResult] = None

    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

    @property
    def current_question(self) -> Optional[str]:
        if self.current_question_idx < len(self.questions):
            return self.questions[self.current_question_idx]
        return None

    @property
    def all_questions_answered(self) -> bool:
        return len(self.questions) > 0 and self.current_question_idx >= len(self.questions)


# ── Request / response schemas exposed by the API ─────────────────────────────

class StartCurationRequest(BaseModel):
    mode:         CurationMode
    provider:     CloudProvider
    service_name: str = ""
    repo_name:    Optional[str] = None
    new_tag:      Optional[str] = None
    description:  str = ""          # Optional free-text seed from user


class SetSourceRequest(BaseModel):
    source_type: str               # "github" | "local"
    url:         Optional[str] = None
    path:        Optional[str] = None
    tag:         Optional[str] = None  # git tag / branch for GitHub source


class AnswerRequest(BaseModel):
    answer: str


class SessionView(BaseModel):
    """Serialisable projection of CurationSession returned to the UI."""
    session_id:              str
    mode:                    CurationMode
    provider:                CloudProvider
    service_name:            str
    status:                  SessionStatus
    questions:               list[str]
    current_question_idx:    int
    qa_pairs:                list[QAPair]
    tf_files_loaded:         list[str]
    registry_docs_available: bool
    local_modules:           list[LocalModuleRef]     = []
    dependent_modules:       list[DependentModuleRef] = []
    followup_round:          int                      = 0
    result:                  Optional[GenerationResult] = None
    error:                   Optional[str]              = None
    current_question:        Optional[str]              = None
    all_questions_answered:  bool                       = False
