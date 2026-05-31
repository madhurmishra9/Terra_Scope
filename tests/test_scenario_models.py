"""
tests/test_scenario_models.py — Round-trip serialization tests for scenario generator models.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.scenario_generator.models import (
    ValidationRule,
    VariableSpec,
    OutputSpec,
    ResourceSpec,
    DataSourceSpec,
    ModuleCallSpec,
    FeatureGate,
    GateType,
    ModuleSpec,
    ScenarioEntry,
    ScenarioPlan,
    GeneratedScenario,
    ValidationResult,
    ScenarioCoverage,
    CoverageReport,
)


# ── VariableSpec ──────────────────────────────────────────────────────────────

def test_variable_spec_required():
    v = VariableSpec(name="project_id", type="string", required=True)
    assert v.required
    assert v.default is None


def test_variable_spec_optional_with_default():
    v = VariableSpec(name="region", type="string", default="us-central1", required=False)
    assert not v.required
    assert v.default == "us-central1"


def test_variable_spec_with_validation():
    rule = ValidationRule(
        condition_expr='contains(["dev","prod"], var.environment)',
        error_message="environment must be dev or prod",
    )
    v = VariableSpec(
        name="environment",
        type="string",
        required=False,
        default="dev",
        validation_rules=[rule],
        enum_values=["dev", "prod"],
    )
    assert len(v.validation_rules) == 1
    assert v.enum_values == ["dev", "prod"]


def test_variable_spec_round_trip():
    v = VariableSpec(
        name="kms_key_name",
        type="string",
        required=False,
        sensitive=True,
        description="KMS key name for CMEK",
    )
    restored = VariableSpec.model_validate(v.model_dump())
    assert restored.name == v.name
    assert restored.sensitive == v.sensitive


def test_variable_spec_json_round_trip():
    v = VariableSpec(name="enable_versioning", type="bool", default=True, required=False)
    restored = VariableSpec.model_validate_json(v.model_dump_json())
    assert restored.default is True


# ── FeatureGate ───────────────────────────────────────────────────────────────

def test_feature_gate_count():
    gate = FeatureGate(
        gate_variable="enable_cmek",
        gate_type=GateType.COUNT,
        gated_resources=["google_kms_key_ring.main", "google_kms_crypto_key.main"],
        on_when="var.enable_cmek == true",
    )
    assert gate.gate_type == GateType.COUNT
    assert len(gate.gated_resources) == 2


def test_feature_gate_for_each():
    gate = FeatureGate(
        gate_variable="replica_regions",
        gate_type=GateType.FOR_EACH,
        gated_resources=["google_storage_bucket.replica"],
        on_when="length(var.replica_regions) > 0",
    )
    assert gate.gate_type == GateType.FOR_EACH


def test_feature_gate_dynamic():
    gate = FeatureGate(
        gate_variable="cors_origins",
        gate_type=GateType.DYNAMIC,
        gated_resources=[],
        gated_blocks=["cors"],
        on_when="var.cors_origins != null",
    )
    assert gate.gate_type == GateType.DYNAMIC
    assert "cors" in gate.gated_blocks


def test_feature_gate_round_trip():
    gate = FeatureGate(
        gate_variable="enable_logging",
        gate_type=GateType.COUNT,
        gated_resources=["google_logging_project_sink.main"],
    )
    restored = FeatureGate.model_validate(gate.model_dump())
    assert restored.gate_variable == gate.gate_variable


# ── ModuleSpec ────────────────────────────────────────────────────────────────

def _make_module_spec() -> ModuleSpec:
    return ModuleSpec(
        module_name="terraform-google-gcs",
        module_source="https://github.com/example/terraform-google-gcs",
        provider="google",
        variables=[
            VariableSpec(name="name", type="string", required=True),
            VariableSpec(name="project_id", type="string", required=True),
            VariableSpec(name="enable_versioning", type="bool", default=False, required=False),
        ],
        outputs=[
            OutputSpec(name="bucket_name", description="GCS bucket name", value_expr="google_storage_bucket.main.name"),
        ],
        resources=[
            ResourceSpec(resource_type="google_storage_bucket", resource_name="main", file_path="main.tf"),
        ],
        feature_gates=[
            FeatureGate(
                gate_variable="enable_versioning",
                gate_type=GateType.DYNAMIC,
                gated_resources=[],
                gated_blocks=["versioning"],
                on_when="var.enable_versioning == true",
            ),
        ],
    )


def test_module_spec_required_variables():
    spec = _make_module_spec()
    req = spec.required_variables
    assert all(v.required for v in req)
    assert any(v.name == "name" for v in req)


def test_module_spec_optional_variables():
    spec = _make_module_spec()
    opt = spec.optional_variables
    assert all(not v.required for v in opt)


def test_module_spec_round_trip():
    spec = _make_module_spec()
    restored = ModuleSpec.model_validate(spec.model_dump())
    assert restored.module_name == spec.module_name
    assert len(restored.variables) == len(spec.variables)
    assert len(restored.feature_gates) == len(spec.feature_gates)


def test_module_spec_json_round_trip():
    spec = _make_module_spec()
    restored = ModuleSpec.model_validate_json(spec.model_dump_json())
    assert restored.provider == "google"


# ── ScenarioPlan ──────────────────────────────────────────────────────────────

def test_scenario_plan_minimal():
    plan = ScenarioPlan(
        module_name="terraform-google-gcs",
        module_source="./",
        scenarios=[
            ScenarioEntry(
                name="minimal",
                description="Required variables only",
                var_values={"name": "test-bucket", "project_id": "my-project"},
                features_expected=["google_storage_bucket.main"],
            ),
        ],
    )
    assert len(plan.scenarios) == 1
    assert plan.scenarios[0].name == "minimal"


def test_scenario_plan_with_expect_failure():
    plan = ScenarioPlan(
        module_name="test",
        module_source="./",
        scenarios=[
            ScenarioEntry(
                name="invalid_env",
                description="Invalid environment value — should fail validation",
                var_values={"environment": "invalid"},
                expect_failure=True,
            ),
        ],
    )
    assert plan.scenarios[0].expect_failure is True


def test_scenario_plan_round_trip():
    plan = ScenarioPlan(
        module_name="test",
        module_source="./",
        scenarios=[
            ScenarioEntry(name="s1", description="test", var_values={"x": "y"}),
            ScenarioEntry(name="s2", description="test2", var_values={"x": "z"}),
        ],
    )
    restored = ScenarioPlan.model_validate_json(plan.model_dump_json())
    assert len(restored.scenarios) == 2


# ── ValidationResult ─────────────────────────────────────────────────────────

def test_validation_result_ok():
    result = ValidationResult(scenario="minimal", ok=True, command="terraform validate")
    assert result.ok
    assert not result.fixed
    assert result.iterations == 0


def test_validation_result_fixed():
    result = ValidationResult(
        scenario="gate_cmek_on",
        ok=True,
        command="terraform validate",
        fixed=True,
        iterations=2,
    )
    assert result.fixed
    assert result.iterations == 2


def test_validation_result_skipped():
    result = ValidationResult(
        scenario="minimal",
        ok=True,
        skipped=True,
        skip_reason="terraform binary not found",
    )
    assert result.skipped
    assert "terraform" in result.skip_reason


def test_validation_result_round_trip():
    result = ValidationResult(scenario="maximal", ok=False, stderr="Error: invalid attribute")
    restored = ValidationResult.model_validate(result.model_dump())
    assert restored.scenario == "maximal"
    assert not restored.ok


# ── CoverageReport ────────────────────────────────────────────────────────────

def test_coverage_report_full_coverage():
    report = CoverageReport(
        module_name="terraform-google-gcs",
        total_scenarios=3,
        passed_scenarios=3,
        per_scenario=[
            ScenarioCoverage(
                scenario_name="minimal",
                variables_set=["name", "project_id"],
                features_exercised=[],
                resources_expected=["google_storage_bucket.main"],
                passed=True,
            ),
        ],
        uncovered_gates=[],
        uncovered_variables=[],
    )
    assert report.passed_scenarios == report.total_scenarios
    assert report.uncovered_gates == []


def test_coverage_report_with_uncovered():
    report = CoverageReport(
        module_name="test",
        total_scenarios=2,
        passed_scenarios=2,
        per_scenario=[],
        uncovered_gates=["enable_cmek"],
        uncovered_variables=["kms_key_name"],
    )
    assert "enable_cmek" in report.uncovered_gates
    assert "kms_key_name" in report.uncovered_variables


def test_coverage_report_round_trip():
    report = CoverageReport(
        module_name="test",
        total_scenarios=1,
        passed_scenarios=1,
        per_scenario=[
            ScenarioCoverage(
                scenario_name="minimal",
                variables_set=["name"],
                features_exercised=["enable_versioning"],
                resources_expected=[],
                passed=True,
            ),
        ],
        uncovered_gates=[],
        uncovered_variables=[],
    )
    restored = CoverageReport.model_validate_json(report.model_dump_json())
    assert restored.module_name == "test"
    assert len(restored.per_scenario) == 1
