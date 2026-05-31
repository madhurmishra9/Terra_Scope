"""
Pydantic v2 models for the scenario generator pipeline.

Every field carries Field(description=...) for self-documentation and
so that LLM agents can use the schema to understand expected structure.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# ── Variable / output / resource specs ────────────────────────────────────────

class ValidationRule(BaseModel):
    """A Terraform variable validation block."""
    condition_expr: str = Field(
        description="The Terraform expression used as the validation condition."
    )
    error_message: str = Field(
        description="The error_message emitted when condition is false."
    )


class VariableSpec(BaseModel):
    """Describes one Terraform input variable parsed from the module."""
    name: str = Field(description="Variable name as declared in variables.tf.")
    type: str = Field(
        default="string",
        description="Terraform type expression (e.g. 'string', 'number', 'bool', 'list(string)').",
    )
    default: Optional[Any] = Field(
        default=None,
        description="Default value from the variable block. None means the variable is required.",
    )
    required: bool = Field(
        description="True when no default is declared (caller must supply the value)."
    )
    nullable: bool = Field(
        default=True,
        description="Mirrors the 'nullable' attribute in the variable block (default: true).",
    )
    sensitive: bool = Field(
        default=False,
        description="True when sensitive = true is set in the variable block.",
    )
    description: str = Field(
        default="",
        description="The description string from the variable block.",
    )
    validation_rules: list[ValidationRule] = Field(
        default_factory=list,
        description="All validation {} blocks for this variable.",
    )
    enum_values: list[str] = Field(
        default_factory=list,
        description="Extracted enum values if a validation uses contains([...], var.x).",
    )


class OutputSpec(BaseModel):
    """Describes one Terraform output parsed from outputs.tf."""
    name: str = Field(description="Output name.")
    description: str = Field(default="", description="Output description string.")
    sensitive: bool = Field(default=False, description="True when sensitive = true.")
    value_expr: str = Field(
        default="",
        description="The raw value expression (e.g. 'google_storage_bucket.main.name').",
    )


class ResourceSpec(BaseModel):
    """Describes one resource block found in the module."""
    resource_type: str = Field(description="Terraform resource type (e.g. 'google_storage_bucket').")
    resource_name: str = Field(description="Logical resource name within the block.")
    file_path: str = Field(description="Source .tf file where this resource is declared.")
    gated_by: Optional[str] = Field(
        default=None,
        description=(
            "Name of the feature-gate variable that controls whether this resource "
            "is created (count, for_each, or dynamic block keyed on a variable). "
            "None if the resource is always created."
        ),
    )


class DataSourceSpec(BaseModel):
    """Describes one data {} block found in the module."""
    data_type: str = Field(description="Terraform data source type.")
    data_name: str = Field(description="Logical name of the data block.")
    file_path: str = Field(description="Source .tf file.")


class ModuleCallSpec(BaseModel):
    """Describes one module {} call found inside the module (nested module call)."""
    module_name: str = Field(description="Logical label of the module block.")
    source: str = Field(description="The source = '...' value.")
    required_inputs: list[str] = Field(
        default_factory=list,
        description="Input variable names that must be set by the caller.",
    )


class GateType(str, Enum):
    COUNT = "count"
    FOR_EACH = "for_each"
    DYNAMIC = "dynamic"


class FeatureGate(BaseModel):
    """A variable-controlled feature gate detected via AST analysis."""
    gate_variable: str = Field(
        description="Name of the variable that enables/disables this feature."
    )
    gate_type: GateType = Field(
        description="Mechanism: count expression, for_each expression, or dynamic block."
    )
    gated_resources: list[str] = Field(
        description="Resource type+name identifiers gated by this variable (e.g. 'google_kms_key_ring.main')."
    )
    gated_blocks: list[str] = Field(
        default_factory=list,
        description="Dynamic block labels gated by this variable.",
    )
    on_when: str = Field(
        default="non_null_or_true",
        description=(
            "Textual description of the condition under which the gate is ON "
            "(e.g. 'var.enable_cmek == true', 'var.replica_count > 0')."
        ),
    )


class ModuleSpec(BaseModel):
    """Complete parsed description of a Terraform module — built deterministically from the AST."""
    module_name: str = Field(description="Human-readable module identifier.")
    module_source: str = Field(description="Original source URL, path, or 'uploaded'.")
    provider: str = Field(
        default="unknown",
        description="Detected primary provider (google | aws | azurerm | unknown).",
    )
    terraform_required_version: str = Field(
        default="",
        description="terraform.required_version constraint from versions.tf.",
    )
    variables: list[VariableSpec] = Field(
        default_factory=list,
        description="All input variables declared in variables.tf.",
    )
    outputs: list[OutputSpec] = Field(
        default_factory=list,
        description="All outputs declared in outputs.tf.",
    )
    resources: list[ResourceSpec] = Field(
        default_factory=list,
        description="All resource blocks found in .tf files.",
    )
    data_sources: list[DataSourceSpec] = Field(
        default_factory=list,
        description="All data blocks found in .tf files.",
    )
    module_calls: list[ModuleCallSpec] = Field(
        default_factory=list,
        description="Nested module{} calls found in the module.",
    )
    feature_gates: list[FeatureGate] = Field(
        default_factory=list,
        description="Feature gates detected via count/for_each/dynamic variable expressions.",
    )

    @property
    def required_variables(self) -> list[VariableSpec]:
        return [v for v in self.variables if v.required]

    @property
    def optional_variables(self) -> list[VariableSpec]:
        return [v for v in self.variables if not v.required]


# ── Scenario planning models ───────────────────────────────────────────────────

class ScenarioEntry(BaseModel):
    """One scenario entry in the ScenarioPlan."""
    name: str = Field(
        description="Short slug identifying this scenario (e.g. 'minimal', 'gate_cmek_on')."
    )
    description: str = Field(
        description="Human-readable description of what this scenario exercises."
    )
    var_values: dict[str, Any] = Field(
        default_factory=dict,
        description="Variable name → value mapping for this scenario. Only required vars must be set.",
    )
    features_expected: list[str] = Field(
        default_factory=list,
        description=(
            "List of resource type+name strings expected to appear in the plan for this scenario "
            "(e.g. 'google_kms_key_ring.main')."
        ),
    )
    expect_failure: bool = Field(
        default=False,
        description=(
            "True for scenarios intentionally designed to fail terraform validate "
            "(e.g. boundary value that violates a validation block)."
        ),
    )


class ScenarioPlan(BaseModel):
    """LLM-produced plan enumerating all scenarios to generate. output_type for the planner Agent."""
    module_name: str = Field(description="Name of the module being tested.")
    module_source: str = Field(description="Source URL or path of the module.")
    scenarios: list[ScenarioEntry] = Field(
        description="Ordered list of scenarios to generate and validate."
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Planner reasoning notes, provider, detected gates, etc.",
    )


# ── Generated scenario models ──────────────────────────────────────────────────

class GeneratedFile(BaseModel):
    """One file in a generated scenario."""
    path: str = Field(description="Relative file path within the scenario directory.")
    content: str = Field(description="Full file content.")


class GeneratedScenario(BaseModel):
    """A fully assembled scenario: root module + tfvars + optional tftest.hcl."""
    name: str = Field(description="Scenario name matching ScenarioEntry.name.")
    based_on_scenario: ScenarioEntry = Field(
        description="The ScenarioEntry this scenario was assembled from."
    )
    files: dict[str, str] = Field(
        description="File path → content for all files in this scenario directory."
    )
    output_dir: str = Field(
        default="",
        description="Absolute path to the directory where scenario files were written.",
    )


# ── Validation models ──────────────────────────────────────────────────────────

class ValidationResult(BaseModel):
    """Result of validating one GeneratedScenario against terraform."""
    scenario: str = Field(description="Scenario name.")
    ok: bool = Field(description="True if the scenario passed all validation checks.")
    command: str = Field(
        default="",
        description="The last terraform command run (e.g. 'terraform validate').",
    )
    stdout: str = Field(default="", description="Combined stdout from the terraform command.")
    stderr: str = Field(default="", description="Stderr / error output from the terraform command.")
    fixed: bool = Field(
        default=False,
        description="True if the fixer agent corrected the config before it passed.",
    )
    iterations: int = Field(
        default=0,
        description="Number of fix iterations attempted (0 = passed on first try).",
    )
    skipped: bool = Field(
        default=False,
        description="True when terraform binary is unavailable and validation was skipped.",
    )
    skip_reason: str = Field(
        default="",
        description="Explanation of why validation was skipped.",
    )


# ── Coverage report ────────────────────────────────────────────────────────────

class ScenarioCoverage(BaseModel):
    """Coverage data for one scenario."""
    scenario_name: str = Field(description="Scenario name.")
    variables_set: list[str] = Field(
        description="Variable names explicitly set in this scenario's var_values."
    )
    features_exercised: list[str] = Field(
        description="Feature gate variables toggled by this scenario."
    )
    resources_expected: list[str] = Field(
        description="Resource identifiers expected to appear in the plan."
    )
    passed: bool = Field(description="Whether this scenario passed validation.")


class CoverageReport(BaseModel):
    """Aggregate coverage across all scenarios."""
    module_name: str = Field(description="Module being tested.")
    total_scenarios: int = Field(description="Total number of generated scenarios.")
    passed_scenarios: int = Field(description="Number of scenarios that passed validation.")
    per_scenario: list[ScenarioCoverage] = Field(
        description="Per-scenario coverage details."
    )
    uncovered_gates: list[str] = Field(
        description=(
            "Feature gate variables from ModuleSpec that are not toggled by any scenario. "
            "An empty list means full gate coverage."
        ),
    )
    uncovered_variables: list[str] = Field(
        description="Variable names not exercised by any scenario.",
    )


# ── Session model for API ──────────────────────────────────────────────────────

class ScenarioSessionStatus(str, Enum):
    LOADING    = "loading"
    PARSING    = "parsing"
    PLANNING   = "planning"
    GENERATING = "generating"
    VALIDATING = "validating"
    DONE       = "done"
    ERROR      = "error"


class ScenarioSession(BaseModel):
    """Server-side session object for the /api/scenarios/* endpoints."""
    session_id: str = Field(description="Unique session identifier.")
    source_type: str = Field(
        default="github",
        description="How the module was provided: github | local | zip | tf.",
    )
    source_url: str = Field(default="", description="GitHub URL or local path.")
    tag: Optional[str] = Field(default=None, description="Git tag or branch.")
    status: ScenarioSessionStatus = Field(default=ScenarioSessionStatus.LOADING)
    error_message: Optional[str] = Field(default=None)
    module_spec: Optional[ModuleSpec] = Field(default=None)
    scenario_plan: Optional[ScenarioPlan] = Field(default=None)
    generated_scenarios: list[GeneratedScenario] = Field(default_factory=list)
    validation_results: list[ValidationResult] = Field(default_factory=list)
    coverage_report: Optional[CoverageReport] = Field(default=None)
    output_dir: str = Field(default="")

    model_config = ConfigDict(use_enum_values=True)
