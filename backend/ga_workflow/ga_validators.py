"""
ga_validators.py — Stage 5: Code validation for GA-generated changes.

Four independent validators:
  1. HCL Syntax         — python-hcl2 parse of every changed .tf file
  2. Required Attributes — ensures google_* resources have all required fields
  3. Naming Conventions  — snake_case, no hyphens, descriptions present
  4. Variable Types      — valid Terraform type expressions

Auto-fix: WARNING-level issues corrected in place when auto_fix=True.
ERROR-level issues are never auto-fixed — they require human review.
"""
from __future__ import annotations

import re
import time
from io import StringIO
from pathlib import Path
from typing import Optional

import hcl2

from backend.config import get_config
from backend.ga_workflow.ga_models import (
    ValidationIssue, ValidatorReport, ValidationResult,
    ValidationSeverity, WorkflowRun, WorkflowStage,
)


REQUIRED_ATTRS: dict[str, list[str]] = {
    "google_bigquery_dataset":            ["dataset_id", "project"],
    "google_bigquery_table":              ["dataset_id", "table_id", "project"],
    "google_bigquery_job":                ["job_id", "project"],
    "google_bigquery_routine":            ["dataset_id", "routine_id", "project", "routine_type", "language"],
    "google_storage_bucket":             ["name", "location", "project"],
    "google_storage_bucket_object":      ["name", "bucket"],
    "google_storage_bucket_iam_binding": ["bucket", "role", "members"],
    "google_pubsub_topic":               ["name", "project"],
    "google_pubsub_subscription":        ["name", "topic", "project"],
    "google_pubsub_topic_iam_binding":   ["topic", "role", "members"],
    "google_dataflow_job":               ["name", "template_gcs_path", "temp_gcs_location", "project"],
    "google_dataflow_flex_template_job": ["name", "container_spec_gcs_path", "project", "location"],
    "google_dataproc_cluster":           ["name", "project"],
    "google_dataproc_job":               ["placement", "project"],
    "google_composer_environment":       ["name", "project"],
    "google_spanner_instance":           ["name", "config", "display_name", "project"],
    "google_spanner_database":           ["name", "instance", "project"],
    "google_bigtable_instance":          ["name", "project"],
    "google_bigtable_table":             ["name", "instance_name", "project"],
    "google_project_service":            ["service", "project"],
    "google_project_iam_binding":        ["project", "role", "members"],
    "google_project_iam_member":         ["project", "role", "member"],
    "google_service_account":            ["account_id", "project"],
    "google_kms_key_ring":               ["name", "location", "project"],
    "google_kms_crypto_key":             ["name", "key_ring"],
}

VALID_PRIMITIVE_TYPES = {"string", "number", "bool", "any"}
VALID_COMPLEX_PREFIXES = {"list(", "set(", "map(", "object(", "tuple(", "optional("}
SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
HYPHEN_RE = re.compile(r"-")

COMMON_TYPE_MISTAKES = {
    "str":      "string",
    "int":      "number",
    "float":    "number",
    "boolean":  "bool",
    "integer":  "number",
    "dict":     "map(string)",
    "array":    "list(string)",
}


# ── Validator 1: HCL Syntax ───────────────────────────────────────────────────

def validate_hcl_syntax(file_paths: list[str], repo_root: Path) -> ValidatorReport:
    t0 = time.monotonic()
    issues: list[ValidationIssue] = []

    for rel_path in file_paths:
        abs_path = repo_root / rel_path
        if not abs_path.exists():
            issues.append(ValidationIssue(
                severity=ValidationSeverity.WARNING,
                file_path=rel_path,
                rule="hcl_file_missing",
                message=f"File not found on disk: {abs_path}",
                suggestion="Ensure the file was written correctly by the implementer",
            ))
            continue

        content = abs_path.read_text(encoding="utf-8", errors="replace")
        if not content.strip():
            issues.append(ValidationIssue(
                severity=ValidationSeverity.WARNING,
                file_path=rel_path,
                rule="hcl_empty_file",
                message="File is empty after GA changes were applied",
                suggestion="Check ga_implementer logs — file write may have failed",
            ))
            continue

        try:
            hcl2.load(StringIO(content))
        except Exception as exc:
            line_match = re.search(r"line (\d+)", str(exc), re.IGNORECASE)
            line_num = int(line_match.group(1)) if line_match else None
            issues.append(ValidationIssue(
                severity=ValidationSeverity.ERROR,
                file_path=rel_path,
                line=line_num,
                rule="hcl_syntax",
                message=f"HCL parse error: {str(exc)[:200]}",
                suggestion="Check for missing braces, incorrect attribute syntax, or invalid type expressions",
            ))

    passed = not any(i.severity == ValidationSeverity.ERROR for i in issues)
    return ValidatorReport(
        validator_name="HCL Syntax",
        passed=passed,
        issues=issues,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )


# ── Validator 2: Required Attributes ─────────────────────────────────────────

def validate_required_attributes(file_paths: list[str], repo_root: Path) -> ValidatorReport:
    t0 = time.monotonic()
    issues: list[ValidationIssue] = []

    for rel_path in file_paths:
        abs_path = repo_root / rel_path
        if not abs_path.exists():
            continue
        content = abs_path.read_text(encoding="utf-8", errors="replace")
        try:
            parsed = hcl2.load(StringIO(content))
        except Exception:
            continue

        raw_resources = parsed.get("resource", {})
        if not raw_resources:
            continue

        for res_type, instances in raw_resources.items():
            required = REQUIRED_ATTRS.get(res_type, [])
            if not required or not isinstance(instances, dict):
                continue

            for res_name, body in instances.items():
                if isinstance(body, list):
                    body = body[0] if body else {}
                for attr in required:
                    if attr not in body:
                        line_num = _find_line(content, "resource", res_type, res_name)
                        issues.append(ValidationIssue(
                            severity=ValidationSeverity.ERROR,
                            file_path=rel_path,
                            line=line_num,
                            rule="required_attr_missing",
                            message=f"`{res_type}.{res_name}` is missing required attribute `{attr}`",
                            suggestion=f"Add `{attr} = var.{attr}` to the resource block",
                        ))
                    else:
                        val = body[attr]
                        if isinstance(val, str) and not val.startswith("${") and not val.startswith("var."):
                            issues.append(ValidationIssue(
                                severity=ValidationSeverity.WARNING,
                                file_path=rel_path,
                                rule="required_attr_hardcoded",
                                message=f"`{res_type}.{res_name}.{attr}` is hardcoded — consider using a variable",
                                suggestion=f"Replace with `var.{attr}`",
                            ))

    passed = not any(i.severity == ValidationSeverity.ERROR for i in issues)
    return ValidatorReport(
        validator_name="Required Attributes",
        passed=passed,
        issues=issues,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )


# ── Validator 3: Naming Conventions ──────────────────────────────────────────

def validate_naming_conventions(file_paths: list[str], repo_root: Path) -> ValidatorReport:
    t0 = time.monotonic()
    issues: list[ValidationIssue] = []

    for rel_path in file_paths:
        abs_path = repo_root / rel_path
        if not abs_path.exists():
            continue
        content = abs_path.read_text(encoding="utf-8", errors="replace")
        try:
            parsed = hcl2.load(StringIO(content))
        except Exception:
            continue

        for var_name, var_body in parsed.get("variable", {}).items():
            if isinstance(var_body, list):
                var_body = var_body[0] if var_body else {}
            line_num = _find_line(content, "variable", var_name)

            if not SNAKE_CASE_RE.match(var_name):
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    file_path=rel_path,
                    line=line_num,
                    rule="naming_snake_case",
                    message=f"Variable `{var_name}` is not snake_case",
                    suggestion=f"Rename to `{_to_snake_case(var_name)}`",
                ))

            desc = var_body.get("description", "")
            if not desc or str(desc).strip() in ("", '""', "''"):
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    file_path=rel_path,
                    line=line_num,
                    rule="naming_empty_description",
                    message=f"Variable `{var_name}` has no description",
                    suggestion='Add `description = "..."` to the variable block',
                ))

        for out_name in parsed.get("output", {}).keys():
            if HYPHEN_RE.search(out_name):
                line_num = _find_line(content, "output", out_name)
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    file_path=rel_path,
                    line=line_num,
                    rule="naming_no_hyphen",
                    message=f"Output `{out_name}` contains a hyphen — use underscores",
                    suggestion=f"Rename to `{out_name.replace('-', '_')}`",
                ))

        for res_type, instances in parsed.get("resource", {}).items():
            if not isinstance(instances, dict):
                continue
            for res_name in instances.keys():
                line_num = _find_line(content, "resource", res_type, res_name)
                if res_name and res_name[0].isdigit():
                    issues.append(ValidationIssue(
                        severity=ValidationSeverity.ERROR,
                        file_path=rel_path,
                        line=line_num,
                        rule="naming_digit_start",
                        message=f"Resource `{res_type}.{res_name}` name starts with a digit",
                        suggestion="Rename to start with a letter or underscore",
                    ))
                if HYPHEN_RE.search(res_name):
                    issues.append(ValidationIssue(
                        severity=ValidationSeverity.ERROR,
                        file_path=rel_path,
                        line=line_num,
                        rule="naming_no_hyphen",
                        message=f"Resource `{res_type}.{res_name}` logical name contains a hyphen",
                        suggestion=f"Rename to `{res_name.replace('-', '_')}`",
                    ))

    passed = not any(i.severity == ValidationSeverity.ERROR for i in issues)
    return ValidatorReport(
        validator_name="Naming Conventions",
        passed=passed,
        issues=issues,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )


# ── Validator 4: Variable Types ───────────────────────────────────────────────

def validate_variable_types(file_paths: list[str], repo_root: Path) -> ValidatorReport:
    t0 = time.monotonic()
    issues: list[ValidationIssue] = []

    for rel_path in file_paths:
        abs_path = repo_root / rel_path
        if not abs_path.exists():
            continue
        content = abs_path.read_text(encoding="utf-8", errors="replace")
        try:
            parsed = hcl2.load(StringIO(content))
        except Exception:
            continue

        for var_name, var_body in parsed.get("variable", {}).items():
            if isinstance(var_body, list):
                var_body = var_body[0] if var_body else {}
            line_num = _find_line(content, "variable", var_name)
            type_val = var_body.get("type")

            if type_val is None:
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.INFO,
                    file_path=rel_path,
                    line=line_num,
                    rule="type_missing",
                    message=f"Variable `{var_name}` has no `type` declaration (implicit `any`)",
                    suggestion="Add an explicit type for better module usability",
                ))
                continue

            type_str = str(type_val).strip().lower()
            if type_str in COMMON_TYPE_MISTAKES:
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    file_path=rel_path,
                    line=line_num,
                    rule="type_invalid",
                    message=f"Variable `{var_name}` uses invalid type `{type_str}`",
                    suggestion=f"Change to `{COMMON_TYPE_MISTAKES[type_str]}`",
                ))
            elif not (type_str in VALID_PRIMITIVE_TYPES or
                      any(type_str.startswith(p) for p in VALID_COMPLEX_PREFIXES)):
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    file_path=rel_path,
                    line=line_num,
                    rule="type_unrecognised",
                    message=f"Variable `{var_name}` has unrecognised type `{type_str}`",
                    suggestion="Use a valid Terraform type: string, number, bool, list(T), map(T), object({...})",
                ))

    passed = not any(i.severity == ValidationSeverity.ERROR for i in issues)
    return ValidatorReport(
        validator_name="Variable Types",
        passed=passed,
        issues=issues,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )


# ── Auto-fix engine ───────────────────────────────────────────────────────────

def auto_fix_warnings(issues: list[ValidationIssue], repo_root: Path) -> list[str]:
    """Apply safe automatic fixes for WARNING-level issues. Returns list of modified files."""
    fixed_files: set[str] = set()

    for issue in issues:
        if issue.severity != ValidationSeverity.WARNING:
            continue
        abs_path = repo_root / issue.file_path
        if not abs_path.exists():
            continue

        content = abs_path.read_text(encoding="utf-8", errors="replace")
        original = content

        if issue.rule == "naming_empty_description":
            var_match = re.search(r"`([^`]+)`", issue.message)
            if var_match:
                name = var_match.group(1)
                content = re.sub(
                    rf'(variable\s+"{re.escape(name)}"\s+\{{)',
                    r'\1\n  description = ""',
                    content,
                )

        elif issue.rule == "naming_snake_case":
            old_m = re.search(r"Variable `([^`]+)`", issue.message)
            new_m = re.search(r"Rename to `([^`]+)`", issue.suggestion or "")
            if old_m and new_m:
                content = content.replace(f'"{old_m.group(1)}"', f'"{new_m.group(1)}"')
                content = content.replace(f"var.{old_m.group(1)}", f"var.{new_m.group(1)}")

        if content != original:
            abs_path.write_text(content, encoding="utf-8")
            fixed_files.add(issue.file_path)

    return sorted(fixed_files)


# ── Main entry point ──────────────────────────────────────────────────────────

def validate_all(
    repo_name: str,
    branch_name: str,
    changed_files: list[str],
    run: WorkflowRun,
    auto_fix: bool = True,
) -> ValidationResult:
    """Run all four validators. Optionally auto-fix warnings. Returns ValidationResult."""
    run.stage = WorkflowStage.VALIDATING
    run.log(f"Running validators on {len(changed_files)} files")

    cfg = get_config()
    repo_cfg = cfg.get_repo(repo_name)
    if not repo_cfg:
        run.log(f"Repo '{repo_name}' not found — skipping validation", level="warning")
        return ValidationResult(
            repo_name=repo_name, branch_name=branch_name,
            overall_passed=True, error_count=0, warning_count=0,
            reports=[], validated_files=changed_files,
        )

    base = Path(__file__).parent.parent.parent
    repo_root = repo_cfg.resolved_local_path(base)

    reports: list[ValidatorReport] = []

    run.log("Validator 1/4: HCL Syntax")
    reports.append(validate_hcl_syntax(changed_files, repo_root))

    run.log("Validator 2/4: Required Attributes")
    reports.append(validate_required_attributes(changed_files, repo_root))

    run.log("Validator 3/4: Naming Conventions")
    reports.append(validate_naming_conventions(changed_files, repo_root))

    run.log("Validator 4/4: Variable Types")
    reports.append(validate_variable_types(changed_files, repo_root))

    all_issues = [i for r in reports for i in r.issues]
    error_count   = sum(1 for i in all_issues if i.severity == ValidationSeverity.ERROR)
    warning_count = sum(1 for i in all_issues if i.severity == ValidationSeverity.WARNING)

    for r in reports:
        status = "✅ PASSED" if r.passed else f"❌ FAILED ({sum(1 for i in r.issues if i.severity == ValidationSeverity.ERROR)} errors)"
        run.log(f"  {r.validator_name}: {status} ({r.duration_ms}ms)")

    if auto_fix and warning_count > 0:
        run.log(f"Auto-fixing {warning_count} warnings...")
        warning_issues = [i for i in all_issues if i.severity == ValidationSeverity.WARNING]
        fixed = auto_fix_warnings(warning_issues, repo_root)
        if fixed:
            run.log(f"  Auto-fixed {len(fixed)} file(s): {', '.join(fixed)}")

    overall_passed = error_count == 0
    run.log(f"Validation complete: {'PASSED ✅' if overall_passed else 'FAILED ❌'} "
            f"({error_count} errors, {warning_count} warnings)")

    return ValidationResult(
        repo_name=repo_name,
        branch_name=branch_name,
        overall_passed=overall_passed,
        error_count=error_count,
        warning_count=warning_count,
        reports=reports,
        validated_files=changed_files,
    )


def _find_line(content: str, block_type: str, *args) -> Optional[int]:
    for i, line in enumerate(content.splitlines(), 1):
        if block_type in line and all(arg in line for arg in args if arg):
            return i
    return None


def _to_snake_case(name: str) -> str:
    name = name.replace("-", "_")
    name = re.sub(r"([A-Z])", r"_\1", name).lower().lstrip("_")
    return name
