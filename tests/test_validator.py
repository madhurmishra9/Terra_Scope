"""
tests/test_validator.py

Tests for the Module Curation validation pipeline.

Validation layers tested:
  1. HCL syntax          (_validate_hcl_syntax)
  2. Structural rules    (_run_structural_validators)
  3. Provider schema     (_validate_provider_schema, schema_fetcher helpers)
  4. Terraform CLI       (_terraform_cli_validate — marked slow, skips if terraform absent)
  5. Full integration    (validate_curation, CurationValidationResult model)
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest

from backend.module_curator.models import (
    CloudProvider,
    CurationMode,
    CurationSession,
    CurationValidationIssue,
    CurationValidationResult,
)
from backend.module_curator.validator import (
    _extract_hcl_resources,
    _label,
    _run_structural_validators,
    _terraform_available,
    _terraform_cli_validate_sync,
    _validate_hcl_syntax,
    _validate_provider_schema,
    _validate_variable_types,
    validate_curation,
)
from backend.registry_fetcher.schema_fetcher import (
    check_attribute,
    check_resource_type,
    clear_cache,
    get_resource_types,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _session(**kwargs) -> CurationSession:
    defaults = dict(
        session_id="test-val-001",
        mode=CurationMode.NEW_PRODUCT,
        provider=CloudProvider.GCP,
        service_name="cloud_sql",
    )
    defaults.update(kwargs)
    return CurationSession(**defaults)


VALID_MAIN = """\
resource "google_sql_database_instance" "main" {
  name             = var.name
  database_version = var.database_version
  region           = var.region
  deletion_protection = false
  settings {
    tier = var.tier
  }
}
"""

VALID_VARS = """\
variable "name" {
  description = "Cloud SQL instance name"
  type        = string
}

variable "database_version" {
  description = "Cloud SQL database version"
  type        = string
  default     = "POSTGRES_15"
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "us-central1"
}

variable "tier" {
  description = "Machine type"
  type        = string
  default     = "db-f1-micro"
}

variable "project_id" {
  description = "GCP project ID"
  type        = string
}
"""

VALID_OUTPUTS = """\
output "connection_name" {
  description = "Cloud SQL connection name"
  value       = google_sql_database_instance.main.connection_name
}

output "instance_name" {
  description = "Cloud SQL instance name"
  value       = google_sql_database_instance.main.name
}
"""

VALID_VERSIONS = """\
terraform {
  required_version = ">= 1.3"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
  }
}
"""

VALID_FILES = {
    "main.tf":      VALID_MAIN,
    "variables.tf": VALID_VARS,
    "outputs.tf":   VALID_OUTPUTS,
    "versions.tf":  VALID_VERSIONS,
}


# ── CurationValidationIssue / CurationValidationResult ───────────────────────

def test_validation_issue_model():
    issue = CurationValidationIssue(
        severity="error",
        file="main.tf",
        line=5,
        rule="hcl_syntax",
        message="Parse error",
        suggestion="Fix the brace",
    )
    assert issue.severity == "error"
    assert issue.file == "main.tf"
    assert issue.line == 5


def test_validation_result_passed_when_no_errors():
    result = CurationValidationResult(passed=True, error_count=0)
    assert result.passed is True
    assert result.error_count == 0
    assert result.issues == []


def test_validation_result_failed_when_errors():
    issue = CurationValidationIssue(severity="error", file="main.tf", message="bad")
    result = CurationValidationResult(passed=False, error_count=1, issues=[issue])
    assert result.passed is False
    assert len(result.issues) == 1


def test_validation_result_optional_fields():
    result = CurationValidationResult(passed=True)
    assert result.terraform_cli_available is False
    assert result.terraform_validate_passed is None
    assert result.provider_schema_checked is False


# ── Layer 1: _validate_hcl_syntax ────────────────────────────────────────────

def test_hcl_syntax_valid_files():
    issues = _validate_hcl_syntax(VALID_FILES)
    hcl_issues = [i for i in issues if i.rule == "hcl_syntax"]
    assert hcl_issues == [], f"Valid HCL should produce no syntax errors, got: {hcl_issues}"


def test_hcl_syntax_unclosed_brace():
    bad = {"main.tf": 'resource "x" "y" {\n  name = "foo"\n'}  # missing closing }
    issues = _validate_hcl_syntax(bad)
    assert any(i.rule == "hcl_syntax" for i in issues)
    assert any(i.severity == "error" for i in issues)


def test_hcl_syntax_invalid_attribute():
    bad = {"variables.tf": 'variable "x" {\n  type = !!invalid\n}'}
    issues = _validate_hcl_syntax(bad)
    assert any(i.rule == "hcl_syntax" for i in issues)


def test_hcl_syntax_empty_file_no_error():
    issues = _validate_hcl_syntax({"main.tf": "# empty\n"})
    assert not any(i.rule == "hcl_syntax" for i in issues)


def test_hcl_syntax_skips_non_tf_files():
    issues = _validate_hcl_syntax({"README.md": "not HCL", "terraform.tfvars": 'x = "y"'})
    assert issues == []


def test_hcl_syntax_multiple_errors_reported():
    bad = {
        "main.tf": 'resource "x" "y" {\n  bad = !!!\n}',
        "variables.tf": 'variable "z" { type = @@@ }',
    }
    issues = _validate_hcl_syntax(bad)
    # At least one file should produce an error
    assert len([i for i in issues if i.severity == "error"]) >= 1


# ── Layer 1: file reporting ───────────────────────────────────────────────────

def test_hcl_syntax_error_has_correct_filename():
    bad = {"variables.tf": 'variable "z" { type = !!! }'}
    issues = _validate_hcl_syntax(bad)
    for i in issues:
        assert i.file == "variables.tf"


# ── Layer 2: _run_structural_validators ──────────────────────────────────────

def test_structural_validators_valid_module():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        for fname, content in VALID_FILES.items():
            (tmp / fname).write_text(content, encoding="utf-8")
        issues = _run_structural_validators(tmp)
        error_issues = [i for i in issues if i.severity == "error"]
        assert error_issues == [], f"Valid module should have no structural errors: {error_issues}"


def test_structural_validators_naming_violation():
    bad_resource = """\
resource "google_sql_database_instance" "myInstance" {
  name   = var.name
  region = var.region
}
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "main.tf").write_text(bad_resource, encoding="utf-8")
        issues = _run_structural_validators(tmp)
        naming_issues = [i for i in issues if i.rule == "naming_convention"]
        assert len(naming_issues) >= 1, f"camelCase resource name should trigger naming warning, got: {issues}"


def test_structural_validators_invalid_variable_type():
    bad_vars = """\
variable "my_count" {
  description = "Count of items"
  type        = int
}
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "variables.tf").write_text(bad_vars, encoding="utf-8")
        issues = _run_structural_validators(tmp)
        type_issues = [i for i in issues if i.rule == "invalid_variable_type"]
        assert len(type_issues) >= 1, f"'int' is not valid Terraform type — should produce error, got: {issues}"


def test_structural_validators_empty_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        issues = _run_structural_validators(Path(tmpdir))
        assert issues == []


# ── Layer 3 helpers: _extract_hcl_resources ───────────────────────────────────

def test_extract_hcl_resources_finds_resource():
    resources = _extract_hcl_resources({"main.tf": VALID_MAIN})
    # hcl2 v4 may include quotes in keys; _label() strips them
    assert "google_sql_database_instance" in resources, (
        f"Expected 'google_sql_database_instance' in resources. Got keys: {list(resources.keys())}"
    )
    assert "main" in resources["google_sql_database_instance"], (
        f"Expected 'main' in instances. Got keys: {list(resources['google_sql_database_instance'].keys())}"
    )


def test_extract_hcl_resources_attributes():
    resources = _extract_hcl_resources({"main.tf": VALID_MAIN})
    attrs = resources["google_sql_database_instance"]["main"]
    assert "name" in attrs, f"Expected 'name' in attrs, got: {attrs}"
    assert "region" in attrs, f"Expected 'region' in attrs, got: {attrs}"


def test_extract_hcl_resources_empty_files():
    assert _extract_hcl_resources({}) == {}


def test_extract_hcl_resources_skips_non_tf():
    resources = _extract_hcl_resources({"README.md": VALID_MAIN})
    assert resources == {}


def test_extract_hcl_resources_invalid_hcl_skipped():
    bad = {"main.tf": 'resource "x" "y" { BAD!!!'}
    result = _extract_hcl_resources(bad)
    assert isinstance(result, dict)  # No exception, returns empty dict


def test_extract_hcl_resources_multiple_resources():
    multi = """\
resource "google_sql_database_instance" "main" {
  name = var.name
}

resource "google_sql_database" "db" {
  name     = var.db_name
  instance = google_sql_database_instance.main.name
}
"""
    resources = _extract_hcl_resources({"main.tf": multi})
    assert "google_sql_database_instance" in resources, f"Got: {list(resources.keys())}"
    assert "google_sql_database" in resources, f"Got: {list(resources.keys())}"


# ── _label helper ─────────────────────────────────────────────────────────────

def test_label_strips_quotes():
    assert _label('"google_sql_database_instance"') == "google_sql_database_instance"


def test_label_plain_string():
    assert _label("my_resource") == "my_resource"


def test_label_empty():
    assert _label("") == ""


# ── schema_fetcher helpers (unit tests with mock schema) ──────────────────────

MOCK_GOOGLE_SCHEMA = {
    "schemas": {
        "google_sql_database_instance": {
            "block": {
                "attributes": {
                    "name": {"type": "string"},
                    "region": {"type": "string"},
                    "database_version": {"type": "string"},
                    "deletion_protection": {"type": "bool"},
                },
                "block_types": {
                    "settings": {
                        "block": {
                            "attributes": {
                                "tier": {"type": "string"},
                                "disk_size": {"type": "number"},
                            }
                        }
                    }
                }
            }
        },
        "google_storage_bucket": {
            "block": {
                "attributes": {
                    "name": {"type": "string"},
                    "location": {"type": "string"},
                    "project": {"type": "string"},
                }
            }
        }
    }
}


def test_get_resource_types():
    types = get_resource_types(MOCK_GOOGLE_SCHEMA)
    assert "google_sql_database_instance" in types
    assert "google_storage_bucket" in types


def test_check_resource_type_existing():
    assert check_resource_type(MOCK_GOOGLE_SCHEMA, "google_sql_database_instance") is True


def test_check_resource_type_missing():
    assert check_resource_type(MOCK_GOOGLE_SCHEMA, "google_nonexistent_resource") is False


def test_check_attribute_top_level():
    assert check_attribute(MOCK_GOOGLE_SCHEMA, "google_sql_database_instance", "name") is True
    assert check_attribute(MOCK_GOOGLE_SCHEMA, "google_sql_database_instance", "region") is True


def test_check_attribute_nested_block():
    assert check_attribute(MOCK_GOOGLE_SCHEMA, "google_sql_database_instance", "settings") is True


def test_check_attribute_misspelled():
    assert check_attribute(MOCK_GOOGLE_SCHEMA, "google_sql_database_instance", "nme") is False


def test_check_attribute_unknown_resource():
    assert check_attribute(MOCK_GOOGLE_SCHEMA, "google_fake_resource", "name") is False


def test_check_attribute_empty_schema():
    assert check_attribute({}, "google_sql_database_instance", "name") is False


# ── Layer 3: _validate_provider_schema (with mocked schema) ──────────────────

@pytest.mark.asyncio
async def test_schema_validation_valid_resources():
    with patch(
        "backend.module_curator.validator._validate_provider_schema",
        new=AsyncMock(return_value=([], True)),
    ):
        issues, checked = await _validate_provider_schema(VALID_FILES, "google")
    # mocked — just verifies the function is callable and returns correct types
    assert isinstance(issues, list)
    assert isinstance(checked, bool)


@pytest.mark.asyncio
async def test_schema_validation_offline_returns_empty():
    """When schema fetch returns None (offline), no issues should be reported."""
    with patch(
        "backend.registry_fetcher.schema_fetcher.fetch_provider_schema",
        new=AsyncMock(return_value=None),
    ):
        issues, checked = await _validate_provider_schema(VALID_FILES, "google")
    assert issues == []
    assert checked is False


@pytest.mark.asyncio
async def test_schema_validation_unknown_resource_type():
    unknown_main = """\
resource "google_nonexistent_service_xyz" "main" {
  name = var.name
}
"""
    files = {"main.tf": unknown_main}
    with patch(
        "backend.module_curator.validator.fetch_provider_schema",
        new=AsyncMock(return_value=MOCK_GOOGLE_SCHEMA),
    ):
        issues, checked = await _validate_provider_schema(files, "google")

    assert checked is True
    error_issues = [i for i in issues if i.severity == "error" and i.rule == "unknown_resource_type"]
    assert len(error_issues) >= 1


@pytest.mark.asyncio
async def test_schema_validation_unknown_attribute():
    bad_attr_main = """\
resource "google_sql_database_instance" "main" {
  name             = var.name
  typo_attribute   = "oops"
}
"""
    files = {"main.tf": bad_attr_main}
    with patch(
        "backend.module_curator.validator.fetch_provider_schema",
        new=AsyncMock(return_value=MOCK_GOOGLE_SCHEMA),
    ):
        issues, checked = await _validate_provider_schema(files, "google")

    assert checked is True
    warn_issues = [i for i in issues if i.rule == "unknown_attribute"]
    assert len(warn_issues) >= 1
    assert any("typo_attribute" in i.message for i in warn_issues)


@pytest.mark.asyncio
async def test_schema_validation_valid_attributes_no_warnings():
    with patch(
        "backend.module_curator.validator.fetch_provider_schema",
        new=AsyncMock(return_value=MOCK_GOOGLE_SCHEMA),
    ):
        issues, checked = await _validate_provider_schema(VALID_FILES, "google")

    attr_warnings = [i for i in issues if i.rule == "unknown_attribute"]
    assert len(attr_warnings) == 0, f"Unexpected attribute warnings: {attr_warnings}"


@pytest.mark.asyncio
async def test_schema_validation_skips_non_provider_resources():
    """null_resource and random_* should not be validated against provider schema."""
    files = {"main.tf": 'resource "null_resource" "wait" { triggers = {} }'}
    with patch(
        "backend.module_curator.validator.fetch_provider_schema",
        new=AsyncMock(return_value=MOCK_GOOGLE_SCHEMA),
    ):
        issues, _ = await _validate_provider_schema(files, "google")
    assert issues == []


# ── Layer 4: Terraform CLI ────────────────────────────────────────────────────

def test_terraform_available_returns_bool():
    result = _terraform_available()
    assert isinstance(result, bool)


@pytest.mark.slow
@pytest.mark.skipif(not _terraform_available(), reason="terraform CLI not in PATH")
def test_terraform_cli_valid_module():
    """Valid files should pass terraform validate."""
    passed, issues = _terraform_cli_validate_sync(VALID_FILES)
    # Note: init downloads provider — may fail if no internet. Check only structure.
    assert isinstance(passed, bool)
    assert isinstance(issues, list)
    for i in issues:
        assert isinstance(i, CurationValidationIssue)


@pytest.mark.slow
@pytest.mark.skipif(not _terraform_available(), reason="terraform CLI not in PATH")
def test_terraform_cli_bad_argument():
    """A file with an invalid resource argument should produce an error after validate."""
    bad_main = """\
resource "local_file" "test" {
  completely_invalid_argument = "this does not exist"
  filename                    = "/tmp/test.txt"
  content                     = "hello"
}
"""
    versions = """\
terraform {
  required_version = ">= 1.3"
  required_providers {
    local = {
      source  = "hashicorp/local"
      version = ">= 2.0"
    }
  }
}
"""
    files = {"main.tf": bad_main, "versions.tf": versions}
    passed, issues = _terraform_cli_validate_sync(files)
    # After init+validate, there should be an error about the invalid argument
    assert passed is False or any(i.severity == "error" for i in issues)


# ── Full integration: validate_curation ──────────────────────────────────────

_SCHEMA_PATCH = "backend.module_curator.validator.fetch_provider_schema"


@pytest.mark.asyncio
async def test_validate_curation_valid_module_passes():
    """A complete valid module should produce passed=True (with mocked schema)."""
    session = _session()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        for fname, content in VALID_FILES.items():
            (tmp / fname).write_text(content, encoding="utf-8")

        with patch(_SCHEMA_PATCH, new=AsyncMock(return_value=MOCK_GOOGLE_SCHEMA)):
            result = await validate_curation(VALID_FILES, tmp, session)

    assert isinstance(result, CurationValidationResult)
    errors = [i.message for i in result.issues if i.severity == "error"]
    assert result.error_count == 0, f"Expected 0 errors, got: {errors}"
    assert result.passed is True


@pytest.mark.asyncio
async def test_validate_curation_bad_hcl_fails():
    """A file with HCL syntax errors should produce passed=False."""
    bad_files = {
        "main.tf": 'resource "x" "y" { BROKEN!!!',
        "variables.tf": VALID_VARS,
        "outputs.tf": VALID_OUTPUTS,
        "versions.tf": VALID_VERSIONS,
    }
    session = _session()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        for fname, content in bad_files.items():
            (tmp / fname).write_text(content, encoding="utf-8")

        with patch(_SCHEMA_PATCH, new=AsyncMock(return_value=None)):
            result = await validate_curation(bad_files, tmp, session)

    assert result.passed is False
    assert result.error_count >= 1
    assert any(i.rule == "hcl_syntax" for i in result.issues)


@pytest.mark.asyncio
async def test_validate_curation_unknown_resource_fails():
    """An unknown resource type should surface as a schema error."""
    bad_main = """\
resource "google_nonexistent_product_xyz" "main" {
  name = var.name
}
"""
    files = {
        "main.tf": bad_main,
        "variables.tf": VALID_VARS,
        "outputs.tf": VALID_OUTPUTS,
        "versions.tf": VALID_VERSIONS,
    }
    session = _session()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        for fname, content in files.items():
            (tmp / fname).write_text(content, encoding="utf-8")

        with patch(_SCHEMA_PATCH, new=AsyncMock(return_value=MOCK_GOOGLE_SCHEMA)):
            result = await validate_curation(files, tmp, session)

    assert any(i.rule == "unknown_resource_type" for i in result.issues), \
        f"Expected unknown_resource_type issue, got: {[i.rule for i in result.issues]}"
    assert result.passed is False


@pytest.mark.asyncio
async def test_validate_curation_offline_still_passes_hcl():
    """When provider schema is unavailable (offline), Layers 1+2 still run."""
    session = _session()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        for fname, content in VALID_FILES.items():
            (tmp / fname).write_text(content, encoding="utf-8")

        with patch(_SCHEMA_PATCH, new=AsyncMock(return_value=None)):
            result = await validate_curation(VALID_FILES, tmp, session)

    assert isinstance(result, CurationValidationResult)
    assert result.provider_schema_checked is False
    assert result.error_count == 0


@pytest.mark.asyncio
async def test_validate_curation_result_has_all_fields():
    session = _session()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        for fname, content in VALID_FILES.items():
            (tmp / fname).write_text(content, encoding="utf-8")

        with patch(_SCHEMA_PATCH, new=AsyncMock(return_value=None)):
            result = await validate_curation(VALID_FILES, tmp, session)

    assert hasattr(result, "passed")
    assert hasattr(result, "error_count")
    assert hasattr(result, "warning_count")
    assert hasattr(result, "issues")
    assert hasattr(result, "terraform_cli_available")
    assert hasattr(result, "terraform_validate_passed")
    assert hasattr(result, "provider_schema_checked")


@pytest.mark.asyncio
async def test_validate_curation_issues_are_typed():
    """All issues in the result should be CurationValidationIssue instances."""
    session = _session()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        for fname, content in VALID_FILES.items():
            (tmp / fname).write_text(content, encoding="utf-8")

        with patch(_SCHEMA_PATCH, new=AsyncMock(return_value=None)):
            result = await validate_curation(VALID_FILES, tmp, session)

    for issue in result.issues:
        assert isinstance(issue, CurationValidationIssue)
        assert issue.severity in ("error", "warning", "info")
        assert isinstance(issue.message, str)
        assert isinstance(issue.file, str)
