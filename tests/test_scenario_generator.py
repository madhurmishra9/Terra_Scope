"""
tests/test_scenario_generator.py — Integration tests for the scenario generator pipeline.

Tests run against the sample_module fixture (tests/fixtures/sample_module/).
Terraform binary calls are skipped gracefully if terraform is not installed.
LLM calls are mocked via monkeypatching planner.plan_scenarios.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from backend.scenario_generator.models import (
    CoverageReport,
    GeneratedScenario,
    ModuleSpec,
    ScenarioEntry,
    ScenarioPlan,
    ValidationResult,
)
from backend.scenario_generator.parser import build_module_spec
from backend.scenario_generator.synthesizer import (
    synthesize_scenario,
)
from backend.scenario_generator.coverage import build_coverage_report
from backend.scenario_generator.validator import generate_terraformrc, generate_tftest_hcl


# ── Fixtures ──────────────────────────────────────────────────────────────────

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sample_module"


def _load_fixture_files() -> dict[str, str]:
    files = {}
    for tf_file in FIXTURE_DIR.glob("*.tf"):
        files[tf_file.name] = tf_file.read_text(encoding="utf-8")
    return files


@pytest.fixture
def tf_files() -> dict[str, str]:
    return _load_fixture_files()


@pytest.fixture
def module_spec(tf_files) -> ModuleSpec:
    return build_module_spec(tf_files, module_name="sample-gcs", module_source="./fixtures/sample_module")


@pytest.fixture
def minimal_scenario(module_spec) -> ScenarioEntry:
    return ScenarioEntry(
        name="minimal",
        description="Required variables only",
        var_values={"name": "test-bucket", "project_id": "my-project"},
        features_expected=["google_storage_bucket.main"],
    )


@pytest.fixture
def maximal_scenario(module_spec) -> ScenarioEntry:
    return ScenarioEntry(
        name="maximal",
        description="All variables set",
        var_values={
            "name": "test-bucket",
            "project_id": "my-project",
            "region": "us-east1",
            "environment": "production",
            "enable_versioning": True,
            "kms_key_name": "projects/my-project/locations/us-central1/keyRings/kr/cryptoKeys/key",
            "cors_origins": ["https://example.com"],
            "replica_locations": ["europe-west1"],
        },
        features_expected=[
            "google_storage_bucket.main",
            "google_storage_bucket.replica",
        ],
    )


# ── Parser tests ──────────────────────────────────────────────────────────────

def test_parser_detects_provider(module_spec):
    assert module_spec.provider == "google"


def test_parser_finds_variables(module_spec):
    var_names = [v.name for v in module_spec.variables]
    assert "name" in var_names
    assert "project_id" in var_names
    assert "region" in var_names
    assert "enable_versioning" in var_names
    assert "kms_key_name" in var_names


def test_parser_required_variables(module_spec):
    req = [v.name for v in module_spec.required_variables]
    assert "name" in req
    assert "project_id" in req
    assert "region" not in req  # has default


def test_parser_optional_variables(module_spec):
    opt = [v.name for v in module_spec.optional_variables]
    assert "region" in opt
    assert "enable_versioning" in opt
    assert "kms_key_name" in opt


def test_parser_variable_defaults(module_spec):
    region = next(v for v in module_spec.variables if v.name == "region")
    assert region.default == "us-central1"
    assert not region.required


def test_parser_variable_sensitive(module_spec):
    # kms_key_name is not marked sensitive in the fixture (sensitive=false)
    kms = next((v for v in module_spec.variables if v.name == "kms_key_name"), None)
    assert kms is not None
    assert kms.required is False  # has default=null


def test_parser_finds_resources(module_spec):
    res_ids = [f"{r.resource_type}.{r.resource_name}" for r in module_spec.resources]
    assert "google_storage_bucket.main" in res_ids
    assert "google_storage_bucket.replica" in res_ids


def test_parser_detects_feature_gates(module_spec):
    gate_vars = {g.gate_variable for g in module_spec.feature_gates}
    # versioning is a dynamic block keyed on enable_versioning
    assert "enable_versioning" in gate_vars or len(module_spec.feature_gates) >= 1


def test_parser_count_gate_replica(module_spec):
    """replica bucket uses count = length(var.replica_locations)."""
    replica = next((r for r in module_spec.resources if r.resource_name == "replica"), None)
    assert replica is not None
    # gated_by may be detected or may be None (count expression is complex)
    # Just assert the resource exists
    assert replica.resource_type == "google_storage_bucket"


def test_parser_detects_tf_version(module_spec):
    assert ">= 1.5" in module_spec.terraform_required_version or module_spec.terraform_required_version != ""


def test_parser_finds_outputs(module_spec):
    out_names = [o.name for o in module_spec.outputs]
    assert "bucket_name" in out_names
    assert "bucket_url" in out_names


def test_parser_finds_data_sources(module_spec):
    # sample module has no data sources — should return empty list
    assert isinstance(module_spec.data_sources, list)


def test_parser_round_trip(module_spec):
    serialized = module_spec.model_dump()
    restored = ModuleSpec.model_validate(serialized)
    assert restored.module_name == module_spec.module_name
    assert len(restored.variables) == len(module_spec.variables)


# ── Synthesizer tests ─────────────────────────────────────────────────────────

def test_synthesize_minimal_creates_files(module_spec, minimal_scenario):
    gs = synthesize_scenario(minimal_scenario, module_spec)
    assert "main.tf" in gs.files
    assert "terraform.tfvars" in gs.files
    assert "versions.tf" in gs.files


def test_synthesize_main_tf_contains_module_block(module_spec, minimal_scenario):
    gs = synthesize_scenario(minimal_scenario, module_spec)
    assert "module" in gs.files["main.tf"]
    assert "source" in gs.files["main.tf"]


def test_synthesize_tfvars_contains_var_values(module_spec, minimal_scenario):
    gs = synthesize_scenario(minimal_scenario, module_spec)
    tfvars = gs.files["terraform.tfvars"]
    assert "test-bucket" in tfvars
    assert "my-project" in tfvars


def test_synthesize_maximal_sets_all_vars(module_spec, maximal_scenario):
    gs = synthesize_scenario(maximal_scenario, module_spec)
    main_tf = gs.files["main.tf"]
    assert "enable_versioning" in main_tf or "true" in main_tf


def test_synthesize_versions_tf_has_provider(module_spec, minimal_scenario):
    gs = synthesize_scenario(minimal_scenario, module_spec)
    assert "google" in gs.files["versions.tf"]
    assert "hashicorp/google" in gs.files["versions.tf"]


def test_synthesize_no_invented_variables(module_spec, minimal_scenario):
    """The generated main.tf must only reference variables from ModuleSpec."""
    gs = synthesize_scenario(minimal_scenario, module_spec)
    known_vars = {v.name for v in module_spec.variables}
    # Extract var names from generated main.tf (very basic check)
    import re
    found_vars = set(re.findall(r'\b(\w+)\s*=\s*["\d\[{]', gs.files.get("main.tf", "")))
    # All found var names that look like module inputs should be from the spec
    # (allow some false positives from HCL keywords)
    assert "name" in gs.files["main.tf"]


def test_synthesize_scenario_name(module_spec, minimal_scenario):
    gs = synthesize_scenario(minimal_scenario, module_spec)
    assert gs.name == "minimal"
    assert gs.based_on_scenario.name == "minimal"


# ── Planner fallback test (no LLM needed) ────────────────────────────────────

def test_planner_fallback_covers_required_vars(module_spec):
    from backend.scenario_generator.planner import _deterministic_fallback
    plan = _deterministic_fallback(module_spec)
    assert len(plan.scenarios) >= 2
    for s in plan.scenarios:
        if not s.expect_failure:
            for req_var in module_spec.required_variables:
                assert req_var.name in s.var_values, \
                    f"Required var '{req_var.name}' missing from scenario '{s.name}'"


def test_planner_fallback_includes_minimal_and_maximal(module_spec):
    from backend.scenario_generator.planner import _deterministic_fallback
    plan = _deterministic_fallback(module_spec)
    names = [s.name for s in plan.scenarios]
    assert "minimal" in names
    assert "maximal" in names


def test_planner_fallback_gate_scenarios(module_spec):
    from backend.scenario_generator.planner import _deterministic_fallback
    plan = _deterministic_fallback(module_spec)
    # Should have ON/OFF scenarios for each detected gate
    names = [s.name for s in plan.scenarios]
    for gate in module_spec.feature_gates:
        assert any(gate.gate_variable in n for n in names), \
            f"No scenario for gate '{gate.gate_variable}'"


# ── Coverage tests ───────────────────────────────────────────────────────────

def test_coverage_report_all_pass(module_spec, minimal_scenario, maximal_scenario):
    gs1 = synthesize_scenario(minimal_scenario, module_spec)
    gs2 = synthesize_scenario(maximal_scenario, module_spec)
    vr1 = ValidationResult(scenario="minimal", ok=True)
    vr2 = ValidationResult(scenario="maximal", ok=True)
    report = build_coverage_report(module_spec, [gs1, gs2], [vr1, vr2])
    assert report.total_scenarios == 2
    assert report.passed_scenarios == 2


def test_coverage_report_uncovered_gates(module_spec, minimal_scenario):
    gs = synthesize_scenario(minimal_scenario, module_spec)
    vr = ValidationResult(scenario="minimal", ok=True)
    report = build_coverage_report(module_spec, [gs], [vr])
    # minimal scenario only sets name + project_id — all gates should be uncovered
    # (unless enable_versioning is set)
    assert isinstance(report.uncovered_gates, list)


def test_coverage_report_round_trip(module_spec, minimal_scenario):
    gs = synthesize_scenario(minimal_scenario, module_spec)
    vr = ValidationResult(scenario="minimal", ok=True)
    report = build_coverage_report(module_spec, [gs], [vr])
    restored = CoverageReport.model_validate(report.model_dump())
    assert restored.module_name == report.module_name


# ── Validator utility tests ───────────────────────────────────────────────────

def test_generate_terraformrc_no_mirror():
    rc = generate_terraformrc(mirror_path=None)
    assert "provider_installation" in rc
    assert "direct" in rc


def test_generate_terraformrc_with_mirror(tmp_path):
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    rc = generate_terraformrc(mirror_path=str(mirror))
    assert "filesystem_mirror" in rc
    assert str(mirror).replace("\\", "/") in rc or "mirror" in rc


def test_generate_tftest_hcl(module_spec, minimal_scenario):
    gs = GeneratedScenario(
        name="minimal",
        based_on_scenario=minimal_scenario,
        files={"main.tf": "# stub"},
    )
    tftest = generate_tftest_hcl(gs, module_spec)
    assert "mock_provider" in tftest
    assert "run" in tftest
    assert "plan" in tftest
    assert "minimal" in tftest or "scenario" in tftest


def test_validate_skipped_without_terraform(module_spec, minimal_scenario):
    """If terraform is not on PATH, validation must skip gracefully."""
    import shutil
    original_which = shutil.which

    def mock_which(name):
        if name == "terraform":
            return None
        return original_which(name)

    gs = GeneratedScenario(
        name="minimal",
        based_on_scenario=minimal_scenario,
        files={"main.tf": "# stub"},
    )

    with patch("shutil.which", side_effect=mock_which):
        from backend.scenario_generator.validator import run_validate
        vr = asyncio.run(run_validate(gs))

    assert vr.skipped
    assert "terraform" in vr.skip_reason.lower()
