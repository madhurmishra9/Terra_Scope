"""
tests/test_code_generator.py

Unit tests for the module curation code generator.
Tests cover:
  - Response parsing (all 4 tiers)
  - HCL content validation
  - Prompt construction
  - Minimal fallback generation
  - Output correctness (no JSON blobs, no arrow functions, required HCL keywords)
"""
import re

import pytest

from backend.module_curator.models import (
    CloudProvider,
    CurationMode,
    CurationSession,
    QAPair,
)
from backend.module_curator.code_generator import (
    _build_prompt,
    _extract_block,
    _extract_file_markers,
    _extract_section,
    _is_valid_hcl_content,
    _minimal_vars,
    _minimal_versions,
    _parse_response,
    _strip_fences,
    _try_parse_json,
    _usage_from_files,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _session(**kwargs) -> CurationSession:
    defaults = dict(
        session_id="test-abc",
        mode=CurationMode.NEW_PRODUCT,
        provider=CloudProvider.GCP,
        service_name="cloud_sql",
    )
    defaults.update(kwargs)
    return CurationSession(**defaults)


MINIMAL_MAIN = 'resource "google_sql_database_instance" "main" {\n  name = var.name\n}\n'
MINIMAL_VARS = 'variable "name" {\n  description = "Instance name"\n  type        = string\n}\n'
MINIMAL_OUTPUTS = 'output "connection_name" {\n  value = google_sql_database_instance.main.connection_name\n}\n'
MINIMAL_VERSIONS = (
    'terraform {\n'
    '  required_version = ">= 1.3"\n'
    '  required_providers {\n'
    '    google = {\n'
    '      source  = "hashicorp/google"\n'
    '      version = ">= 5.0"\n'
    '    }\n'
    '  }\n'
    '}\n'
)


# ── _strip_fences ─────────────────────────────────────────────────────────────

def test_strip_fences_json():
    assert _strip_fences("```json\n{}\n```") == "{}"


def test_strip_fences_hcl():
    raw = "```hcl\nresource \"x\" \"y\" {}\n```"
    assert "```" not in _strip_fences(raw)
    assert "resource" in _strip_fences(raw)


def test_strip_fences_noop():
    raw = 'variable "x" { type = string }'
    assert _strip_fences(raw) == raw


# ── _try_parse_json ───────────────────────────────────────────────────────────

def test_try_parse_json_valid():
    raw = '{"files": {"main.tf": "resource \\"x\\" \\"y\\" {}"}}'
    result = _try_parse_json(raw)
    assert result is not None
    assert "files" in result


def test_try_parse_json_with_fences():
    raw = '```json\n{"files": {}}\n```'
    result = _try_parse_json(raw)
    assert result == {"files": {}}


def test_try_parse_json_embedded_in_text():
    raw = 'Here is the output:\n{"files": {"main.tf": "terraform {}"}}\nEnd.'
    result = _try_parse_json(raw)
    assert result is not None


def test_try_parse_json_invalid_returns_none():
    assert _try_parse_json("this is not json at all") is None
    assert _try_parse_json("") is None


# ── _extract_file_markers ─────────────────────────────────────────────────────

MARKER_RESPONSE = """\
[FILE: main.tf]
resource "google_sql_database_instance" "main" {
  name = var.name
}
[/FILE]
[FILE: variables.tf]
variable "name" {
  description = "Instance name"
  type        = string
}
[/FILE]
[FILE: outputs.tf]
output "connection_name" {
  value = google_sql_database_instance.main.connection_name
}
[/FILE]
[FILE: versions.tf]
terraform {
  required_version = ">= 1.3"
}
[/FILE]
[SUMMARY]
A Cloud SQL database module for GCP.
[/SUMMARY]
[USAGE]
module "db" {
  source = "./"
  name   = "my-db"
}
[/USAGE]
"""


def test_extract_file_markers_all_four_files():
    files = _extract_file_markers(MARKER_RESPONSE)
    assert set(files.keys()) == {"main.tf", "variables.tf", "outputs.tf", "versions.tf"}


def test_extract_file_markers_content_correct():
    files = _extract_file_markers(MARKER_RESPONSE)
    assert "google_sql_database_instance" in files["main.tf"]
    assert 'variable "name"' in files["variables.tf"]
    assert 'output "connection_name"' in files["outputs.tf"]
    assert "required_version" in files["versions.tf"]


def test_extract_file_markers_empty_response():
    assert _extract_file_markers("no markers here") == {}


def test_extract_file_markers_partial():
    raw = "[FILE: main.tf]\nresource \"x\" \"y\" {}\n[/FILE]"
    files = _extract_file_markers(raw)
    assert "main.tf" in files
    assert "variables.tf" not in files


# ── _extract_section ──────────────────────────────────────────────────────────

def test_extract_section_summary():
    summary = _extract_section(MARKER_RESPONSE, "SUMMARY")
    assert summary == "A Cloud SQL database module for GCP."


def test_extract_section_usage():
    usage = _extract_section(MARKER_RESPONSE, "USAGE")
    assert "module" in usage
    assert "source" in usage


def test_extract_section_missing():
    assert _extract_section("no section here", "SUMMARY") == ""


# ── _is_valid_hcl_content ─────────────────────────────────────────────────────

def test_valid_hcl_main_tf():
    assert _is_valid_hcl_content(MINIMAL_MAIN, "main.tf") is True


def test_valid_hcl_variables_tf():
    assert _is_valid_hcl_content(MINIMAL_VARS, "variables.tf") is True


def test_valid_hcl_outputs_tf():
    assert _is_valid_hcl_content(MINIMAL_OUTPUTS, "outputs.tf") is True


def test_valid_hcl_versions_tf():
    assert _is_valid_hcl_content(MINIMAL_VERSIONS, "versions.tf") is True


def test_invalid_hcl_json_blob():
    json_blob = '{"files": {"main.tf": "resource ..."}}'
    assert _is_valid_hcl_content(json_blob, "main.tf") is False


def test_invalid_hcl_arrow_function():
    arrow = 'output["_"].instance_name = (values) => values.key'
    assert _is_valid_hcl_content(arrow, "outputs.tf") is False


def test_invalid_hcl_empty():
    assert _is_valid_hcl_content("", "main.tf") is False
    assert _is_valid_hcl_content("   ", "variables.tf") is False


def test_invalid_hcl_main_tf_no_resource_keyword():
    assert _is_valid_hcl_content("# just a comment", "main.tf") is False


def test_valid_hcl_outputs_tf_comment_only():
    assert _is_valid_hcl_content("# No outputs generated\n", "outputs.tf") is True


# ── _extract_block (legacy markers) ──────────────────────────────────────────

LEGACY_RESPONSE = """\
--- main.tf ---
resource "google_sql_database_instance" "main" {
  name = var.name
}
--- variables.tf ---
variable "name" {
  description = "Instance name"
  type        = string
}
--- outputs.tf ---
output "id" {
  value = google_sql_database_instance.main.id
}
--- versions.tf ---
terraform {
  required_version = ">= 1.3"
}
"""


def test_extract_block_main_tf():
    block = _extract_block(LEGACY_RESPONSE, "main.tf")
    assert "google_sql_database_instance" in block


def test_extract_block_variables_tf():
    block = _extract_block(LEGACY_RESPONSE, "variables.tf")
    assert 'variable "name"' in block


def test_extract_block_missing():
    assert _extract_block("nothing here", "main.tf") == ""


# ── _minimal_vars ─────────────────────────────────────────────────────────────

def test_minimal_vars_gcp():
    session = _session(provider=CloudProvider.GCP)
    result = _minimal_vars(session)
    assert 'variable "project_id"' in result
    assert 'variable "region"' in result
    assert "us-central1" in result


def test_minimal_vars_aws():
    session = _session(provider=CloudProvider.AWS)
    result = _minimal_vars(session)
    assert 'variable "project_id"' in result
    assert 'variable "region"' in result
    assert "us-east-1" in result


def test_minimal_vars_azure():
    session = _session(provider=CloudProvider.AZURE)
    result = _minimal_vars(session)
    assert 'variable "project_id"' in result
    assert 'variable "location"' in result
    assert "East US" in result


def test_minimal_vars_is_valid_hcl():
    for provider in CloudProvider:
        session = _session(provider=provider)
        result = _minimal_vars(session)
        assert _is_valid_hcl_content(result, "variables.tf"), \
            f"_minimal_vars for {provider} is not valid HCL"


# ── _minimal_versions ─────────────────────────────────────────────────────────

def test_minimal_versions_google():
    session = _session(provider=CloudProvider.GCP)
    result = _minimal_versions(session)
    assert "hashicorp/google" in result
    assert ">= 5.0" in result
    assert ">= 1.3" in result


def test_minimal_versions_aws():
    session = _session(provider=CloudProvider.AWS)
    result = _minimal_versions(session)
    assert "hashicorp/aws" in result
    assert ">= 5.0" in result


def test_minimal_versions_azure():
    session = _session(provider=CloudProvider.AZURE)
    result = _minimal_versions(session)
    assert "hashicorp/azurerm" in result
    assert ">= 3.0" in result


def test_minimal_versions_is_valid_hcl():
    for provider in CloudProvider:
        session = _session(provider=provider)
        result = _minimal_versions(session)
        assert _is_valid_hcl_content(result, "versions.tf"), \
            f"_minimal_versions for {provider} is not valid HCL"


# ── _parse_response — Tier 1: file markers ────────────────────────────────────

def test_parse_response_tier1_file_markers():
    session = _session()
    files, summary, usage = _parse_response(MARKER_RESPONSE, session)
    assert "main.tf" in files
    assert "variables.tf" in files
    assert "outputs.tf" in files
    assert "versions.tf" in files
    assert "google_sql_database_instance" in files["main.tf"]


def test_parse_response_tier1_summary_extracted():
    session = _session()
    _, summary, _ = _parse_response(MARKER_RESPONSE, session)
    assert summary == "A Cloud SQL database module for GCP."


def test_parse_response_tier1_usage_extracted():
    session = _session()
    _, _, usage = _parse_response(MARKER_RESPONSE, session)
    assert "module" in usage


# ── _parse_response — Tier 2: JSON format ────────────────────────────────────

VALID_JSON_RESPONSE = """\
{
  "files": {
    "main.tf": "resource \\"google_sql_database_instance\\" \\"main\\" {\\n  name = var.name\\n}\\n",
    "variables.tf": "variable \\"name\\" {\\n  description = \\"Instance name\\"\\n  type        = string\\n}\\n",
    "outputs.tf": "output \\"connection_name\\" {\\n  value = google_sql_database_instance.main.connection_name\\n}\\n",
    "versions.tf": "terraform {\\n  required_version = \\">= 1.3\\"\\n}\\n"
  },
  "summary": "Cloud SQL module",
  "usage_example": "module \\"db\\" { source = \\".//\\" }"
}
"""


def test_parse_response_tier2_valid_json():
    session = _session()
    files, summary, usage = _parse_response(VALID_JSON_RESPONSE, session)
    assert "main.tf" in files
    assert "google_sql_database_instance" in files["main.tf"]
    assert summary == "Cloud SQL module"


def test_parse_response_tier2_json_invalid_hcl_filtered():
    """JSON files with invalid HCL (arrow functions, JSON dumps) are filtered out."""
    bad_json = """\
{
  "files": {
    "main.tf": "resource \\"x\\" \\"y\\" { name = var.n }",
    "outputs.tf": "output[\\"_\\"].x = (v) => v.key"
  },
  "summary": "test"
}
"""
    session = _session()
    files, _, _ = _parse_response(bad_json, session)
    assert "main.tf" in files
    assert "outputs.tf" not in files


# ── _parse_response — Tier 3: legacy markers ─────────────────────────────────

def test_parse_response_tier3_legacy_markers():
    session = _session()
    files, summary, _ = _parse_response(LEGACY_RESPONSE, session)
    assert "main.tf" in files
    assert "variables.tf" in files
    assert "google_sql_database_instance" in files["main.tf"]
    assert summary == f"Terraform module for {session.service_name}"


# ── _parse_response — Tier 4: minimal fallback ───────────────────────────────

GARBAGE_RESPONSE = "Here is some completely unstructured text with no terraform code at all."


def test_parse_response_tier4_produces_all_four_files():
    session = _session()
    files, _, _ = _parse_response(GARBAGE_RESPONSE, session)
    assert "main.tf" in files
    assert "variables.tf" in files
    assert "outputs.tf" in files
    assert "versions.tf" in files


def test_parse_response_tier4_variables_are_valid_hcl():
    session = _session(provider=CloudProvider.GCP)
    files, _, _ = _parse_response(GARBAGE_RESPONSE, session)
    assert _is_valid_hcl_content(files["variables.tf"], "variables.tf")


def test_parse_response_tier4_versions_are_valid_hcl():
    session = _session(provider=CloudProvider.GCP)
    files, _, _ = _parse_response(GARBAGE_RESPONSE, session)
    assert _is_valid_hcl_content(files["versions.tf"], "versions.tf")


def test_parse_response_tier4_main_not_raw_json_dump():
    """The old code dumped raw JSON into main.tf. The new code must not do this."""
    raw_json_dump = '{"files": {"main.tf": "resource \\"x\\" \\"y\\" {}"}}'
    session = _session()
    files, _, _ = _parse_response(raw_json_dump, session)
    # If JSON parsed successfully (tier 2), main.tf should have real HCL
    # If it fell through to tier 4, main.tf must not be a raw JSON blob
    content = files.get("main.tf", "")
    has_json_dump = content.strip().startswith("{") and '"files"' in content
    assert not has_json_dump, "main.tf must not contain a raw JSON dump"


# ── Real-world failure: the memorystore valkey case ───────────────────────────

VALKEY_BROKEN_MAIN = """\
{
  "files": {
    "versions.tf": "terraform {\\n  required_version = \\">= 1.0\\"\\n}\\n",
    "variables.tf": "---\\nvariable \\"project_id\\" {\\n  type = string\\n}\\n",
    "main.tf": "resource \\"google_redis_instance\\" \\"valkey\\" {\\n  name = var.name\\n}\\n",
    "outputs.tf": "output[\\"_\\"].instance_name = (values) => values.key\\n"
  }
}
"""


def test_valkey_case_main_tf_extracted():
    """Reproduce the Memorystore Valkey failure: JSON with some valid and some invalid files."""
    session = _session(service_name="memorystore_valkey")
    files, _, _ = _parse_response(VALKEY_BROKEN_MAIN, session)
    assert "main.tf" in files
    assert "google_redis_instance" in files["main.tf"]


def test_valkey_case_arrow_function_outputs_filtered():
    """outputs.tf with arrow functions must be excluded, not written to disk."""
    session = _session(service_name="memorystore_valkey")
    files, _, _ = _parse_response(VALKEY_BROKEN_MAIN, session)
    if "outputs.tf" in files:
        assert "=>" not in files["outputs.tf"] or 'output "' in files["outputs.tf"]


def test_valkey_case_no_json_in_main_tf():
    """main.tf must never be a raw JSON blob."""
    session = _session(service_name="memorystore_valkey")
    files, _, _ = _parse_response(VALKEY_BROKEN_MAIN, session)
    content = files.get("main.tf", "")
    assert not (content.strip().startswith("{") and '"files"' in content)


# ── _build_prompt ─────────────────────────────────────────────────────────────

def test_build_prompt_contains_provider():
    session = _session(provider=CloudProvider.GCP)
    prompt = _build_prompt(session)
    assert "google" in prompt


def test_build_prompt_contains_service_name():
    session = _session(service_name="cloud_sql")
    prompt = _build_prompt(session)
    assert "cloud_sql" in prompt


def test_build_prompt_contains_file_markers():
    session = _session()
    prompt = _build_prompt(session)
    assert "[FILE: main.tf]" in prompt
    assert "[/FILE]" in prompt
    assert "[SUMMARY]" in prompt


def test_build_prompt_no_json_format():
    session = _session()
    prompt = _build_prompt(session)
    assert '"files"' not in prompt, "Prompt should not request JSON format"


def test_build_prompt_includes_registry_docs():
    session = _session(registry_docs="## google_redis_instance\nThe Redis instance resource.")
    prompt = _build_prompt(session)
    assert "PROVIDER DOCUMENTATION" in prompt
    assert "google_redis_instance" in prompt


def test_build_prompt_includes_qa_pairs():
    session = _session(
        qa_pairs=[QAPair(question="Which region?", answer="us-central1")]
    )
    prompt = _build_prompt(session)
    assert "USER REQUIREMENTS" in prompt
    assert "Which region?" in prompt
    assert "us-central1" in prompt


def test_build_prompt_includes_existing_tf_files():
    session = _session(tf_files={"main.tf": MINIMAL_MAIN})
    prompt = _build_prompt(session)
    assert "EXISTING CODE" in prompt
    assert "google_sql_database_instance" in prompt


def test_build_prompt_includes_document_text():
    session = _session(document_text="Deploy a Redis instance with HA mode.")
    prompt = _build_prompt(session)
    assert "SPECIFICATION" in prompt or "SPEC" in prompt
    assert "Redis instance" in prompt


def test_build_prompt_rules_present():
    session = _session()
    prompt = _build_prompt(session)
    # New prompt uses HCL RULES section with FORBIDDEN keyword
    assert "forbidden" in prompt.lower() or "hardcode" in prompt.lower() or "hcl rules" in prompt.lower()


# ── _usage_from_files ─────────────────────────────────────────────────────────

def test_usage_from_files_is_valid_hcl_snippet():
    files = {"main.tf": MINIMAL_MAIN, "variables.tf": MINIMAL_VARS}
    usage = _usage_from_files(files)
    assert "module" in usage
    assert "source" in usage


# ── Output file correctness checks ───────────────────────────────────────────

def test_all_parse_tiers_produce_versions_tf_with_required_version():
    responses = [MARKER_RESPONSE, VALID_JSON_RESPONSE, LEGACY_RESPONSE, GARBAGE_RESPONSE]
    session = _session(provider=CloudProvider.GCP)
    for i, raw in enumerate(responses):
        files, _, _ = _parse_response(raw, session)
        assert "versions.tf" in files, f"Tier {i+1} missing versions.tf"
        assert "required_version" in files["versions.tf"] or "terraform" in files["versions.tf"], \
            f"Tier {i+1} versions.tf missing terraform block"


def test_all_parse_tiers_produce_variables_tf_with_variable_blocks():
    session = _session(provider=CloudProvider.GCP)
    responses_tiers = [
        (MARKER_RESPONSE, 1),
        (VALID_JSON_RESPONSE, 2),
        (LEGACY_RESPONSE, 3),
        (GARBAGE_RESPONSE, 4),
    ]
    for raw, tier in responses_tiers:
        files, _, _ = _parse_response(raw, session)
        content = files.get("variables.tf", "")
        assert "variable" in content, f"Tier {tier} variables.tf missing variable blocks"


def test_generated_main_tf_has_no_arrow_functions():
    for raw in [MARKER_RESPONSE, VALID_JSON_RESPONSE, LEGACY_RESPONSE]:
        session = _session()
        files, _, _ = _parse_response(raw, session)
        content = files.get("main.tf", "")
        assert not re.search(r"=\s*\([\w,\s]+\)\s*=>", content), \
            "main.tf must not contain JavaScript arrow function syntax"


def test_generated_outputs_tf_has_no_arrow_functions():
    """Arrow function syntax in outputs.tf must either be absent or filtered out."""
    arrow_outputs = "[FILE: outputs.tf]\noutput[\"_\"].x = (v) => v.key\n[/FILE]"
    session = _session()
    files, _, _ = _parse_response(arrow_outputs, session)
    if "outputs.tf" in files:
        content = files["outputs.tf"]
        assert not re.search(r"=\s*\([\w,\s]+\)\s*=>", content), \
            "outputs.tf must not contain arrow function syntax"


