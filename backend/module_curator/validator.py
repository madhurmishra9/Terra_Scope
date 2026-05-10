"""
validator.py — Post-generation validation for the Module Curation pipeline.

Four validation layers (in order):
  1. HCL parse         — hcl2.load() on each generated file (offline, instant)
  2. Structural rules  — naming conventions, variable types, required attributes
                         (direct hcl2 v4 implementation — ga_validators use older API)
  3. Provider schema   — registry.terraform.io: validate resource types and attribute names
  4. Terraform CLI     — terraform init -backend=false + terraform validate -json (optional, slow)

All layers degrade gracefully: network failures skip Layer 3, missing CLI skips Layer 4.
Validation NEVER blocks code generation — results are attached to GenerationResult.validation.
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import tempfile
from io import StringIO
from pathlib import Path
from typing import Iterable, Optional

import hcl2

from backend.registry_fetcher.schema_fetcher import (
    check_attribute,
    check_resource_type,
    fetch_provider_schema,
)
from backend.module_curator.models import (
    CurationSession,
    CurationValidationIssue,
    CurationValidationResult,
)

# Terraform meta-arguments — never provider-specific attributes
_META_ARGS = frozenset({
    "for_each", "count", "depends_on", "lifecycle", "provider",
    "connection", "provisioner", "timeouts", "__is_block__",
})

# Provider resource prefixes eligible for schema validation
_PROVIDER_PREFIXES = ("google_", "aws_", "azurerm_")

# Valid Terraform primitive types
_VALID_PRIMITIVE_TYPES = frozenset({"string", "number", "bool", "any"})
_VALID_COMPLEX_PREFIXES = ("list(", "set(", "map(", "object(", "tuple(", "optional(")
_TYPE_CORRECTIONS = {
    "str": "string", "int": "number", "float": "number",
    "boolean": "bool", "dict": "map(string)", "array": "list(string)",
}


# ── hcl2 v4 compatibility helpers ────────────────────────────────────────────

def _label(raw: str) -> str:
    """Strip surrounding quotes from hcl2 v4 block label strings."""
    return raw.strip('"')


def _iter_block_attrs(val: object) -> Iterable[dict]:
    """
    Normalise hcl2 v4 block values to an iterable of attribute dicts.

    hcl2 v4 wraps block bodies in a list:  name -> [{"attr": value}]
    Older hcl2 uses a plain dict:           name -> {"attr": value}
    """
    if isinstance(val, list):
        for item in val:
            if isinstance(item, dict):
                yield item
    elif isinstance(val, dict):
        yield val


def _flat_attrs(val: object) -> dict:
    """Merge all attribute dicts from a block value into one flat dict."""
    merged: dict = {}
    for d in _iter_block_attrs(val):
        merged.update(d)
    return merged


# ── Layer 1: HCL syntax ───────────────────────────────────────────────────────

def _validate_hcl_syntax(files: dict[str, str]) -> list[CurationValidationIssue]:
    """Parse each .tf file with hcl2 and collect syntax errors."""
    issues: list[CurationValidationIssue] = []
    for fname, content in files.items():
        if not fname.endswith(".tf"):
            continue
        try:
            hcl2.load(StringIO(content))
        except Exception as exc:
            msg = str(exc)
            line: Optional[int] = None
            m = re.search(r"line (\d+)", msg, re.I)
            if m:
                line = int(m.group(1))
            issues.append(CurationValidationIssue(
                severity="error",
                file=fname,
                line=line,
                rule="hcl_syntax",
                message=f"HCL parse error: {msg[:300]}",
                suggestion="Check for unclosed braces, missing quotes, or invalid attribute syntax.",
            ))
    return issues


# ── Layer 1b: Inline flag extraction ─────────────────────────────────────────

_FLAG_PATTERNS = [
    (re.compile(r"#\s*⚠️\s*SECURITY:\s*(.+)"),  "security_flag",  "warning"),
    (re.compile(r"#\s*💰\s*COST:\s*(.+)"),        "cost_flag",      "info"),
    (re.compile(r"#\s*🔧\s*TFLINT:\s*(.+)"),      "tflint_flag",    "info"),
]


def _extract_inline_flags(files: dict[str, str]) -> list[CurationValidationIssue]:
    """Surface ⚠️ SECURITY / 💰 COST / 🔧 TFLINT comments as structured issues."""
    issues: list[CurationValidationIssue] = []
    for fname, content in files.items():
        for lineno, line in enumerate(content.splitlines(), 1):
            for pattern, rule, sev in _FLAG_PATTERNS:
                m = pattern.search(line)
                if m:
                    issues.append(CurationValidationIssue(
                        severity=sev,
                        file=fname,
                        line=lineno,
                        rule=rule,
                        message=m.group(1).strip(),
                    ))
    return issues


# ── Layer 1c: Security heuristics ────────────────────────────────────────────

_OPEN_CIDR_RE = re.compile(r'(?:0\.0\.0\.0/0|::/0)')
_HARDCODED_SECRET_RE = re.compile(r'(?:password|secret|token|key)\s*=\s*"[^${}][^"]{3,}"', re.I)
_HARDCODED_ACCOUNT_RE = re.compile(r'"[0-9]{12}"')

_RESOURCE_SECURITY_CHECKS: list[tuple[str, str, str, str]] = [
    # (resource_type_substring, required_attr_substring, rule, message)
    ("google_storage_bucket",    "uniform_bucket_level_access",
     "gcs_access_control", "GCS bucket missing uniform_bucket_level_access = true."),
    ("google_sql_database",      "deletion_protection",
     "no_deletion_protection", "Cloud SQL instance missing deletion_protection = true."),
    ("google_sql_database",      "require_ssl",
     "sql_no_ssl", "Cloud SQL instance missing require_ssl = true."),
    ("aws_db_instance",          "deletion_protection",
     "no_deletion_protection", "RDS instance missing deletion_protection = true."),
    ("aws_db_instance",          "storage_encrypted",
     "unencrypted_storage", "RDS instance missing storage_encrypted = true."),
    ("aws_s3_bucket",            "block_public_acls",
     "s3_public_access", "S3 bucket missing public access block settings."),
    ("azurerm_storage_account",  "https_traffic_only_enabled",
     "storage_no_https", "Azure storage account missing https_traffic_only_enabled = true."),
]


def _validate_security(files: dict[str, str]) -> list[CurationValidationIssue]:
    """Heuristic security checks on generated HCL content."""
    issues: list[CurationValidationIssue] = []

    for fname, content in files.items():
        if not fname.endswith(".tf"):
            continue

        # Open CIDR ingress
        for m in _OPEN_CIDR_RE.finditer(content):
            lineno = content[: m.start()].count("\n") + 1
            issues.append(CurationValidationIssue(
                severity="warning",
                file=fname,
                line=lineno,
                rule="open_ingress",
                message="Unrestricted CIDR (0.0.0.0/0 or ::/0) detected.",
                suggestion="Restrict to specific CIDR ranges via a variable.",
            ))

        # Hardcoded secrets
        for m in _HARDCODED_SECRET_RE.finditer(content):
            lineno = content[: m.start()].count("\n") + 1
            issues.append(CurationValidationIssue(
                severity="error",
                file=fname,
                line=lineno,
                rule="hardcoded_secret",
                message=f"Potential hardcoded secret: {m.group(0)[:50]}",
                suggestion="Use var.* with sensitive=true or a secrets manager reference.",
            ))

        # Hardcoded AWS account IDs
        for m in _HARDCODED_ACCOUNT_RE.finditer(content):
            lineno = content[: m.start()].count("\n") + 1
            issues.append(CurationValidationIssue(
                severity="warning",
                file=fname,
                line=lineno,
                rule="hardcoded_account_id",
                message=f"Potential hardcoded account ID: {m.group(0)}",
                suggestion="Use data.aws_caller_identity.current.account_id instead.",
            ))

        # Resource-specific attribute checks
        for res_substr, attr_substr, rule, msg in _RESOURCE_SECURITY_CHECKS:
            if res_substr in content and attr_substr not in content:
                issues.append(CurationValidationIssue(
                    severity="warning",
                    file=fname,
                    rule=rule,
                    message=msg,
                    suggestion=f"Add {attr_substr} = true to follow security best practices.",
                ))

    return issues


# ── Layer 2: Structural rules ─────────────────────────────────────────────────

def _validate_variable_types(fname: str, parsed: dict) -> list[CurationValidationIssue]:
    issues: list[CurationValidationIssue] = []
    for var_block in parsed.get("variable", []):
        if not isinstance(var_block, dict):
            continue
        for var_name_raw, var_val in var_block.items():
            var_name = _label(var_name_raw)
            attrs = _flat_attrs(var_val)

            # Description check
            desc = attrs.get("description", "")
            if not (isinstance(desc, str) and desc.strip()):
                issues.append(CurationValidationIssue(
                    severity="warning",
                    file=fname,
                    rule="missing_description",
                    message=f"Variable '{var_name}' has no description.",
                    suggestion="Add a description for terraform-docs compatibility.",
                ))

            # Type check
            var_type = attrs.get("type")
            if var_type is None:
                continue
            if not isinstance(var_type, str):
                continue
            t = var_type.strip()
            if not t:
                continue
            if t in _VALID_PRIMITIVE_TYPES:
                continue
            if any(t.startswith(p) for p in _VALID_COMPLEX_PREFIXES):
                continue
            correction = _TYPE_CORRECTIONS.get(t)
            suggestion = f"Did you mean '{correction}'?" if correction else "Use a valid Terraform type (string, number, bool, list(...), etc.)."
            issues.append(CurationValidationIssue(
                severity="error",
                file=fname,
                rule="invalid_variable_type",
                message=f"Variable '{var_name}' has invalid type '{t}'.",
                suggestion=suggestion,
            ))
    return issues


def _validate_naming(fname: str, parsed: dict) -> list[CurationValidationIssue]:
    issues: list[CurationValidationIssue] = []
    snake_re = re.compile(r"^[a-z][a-z0-9_]*$")

    for resource_block in parsed.get("resource", []):
        if not isinstance(resource_block, dict):
            continue
        for res_type_raw, res_val in resource_block.items():
            res_type = _label(res_type_raw)
            for instance_item in _iter_block_attrs(res_val):
                for res_name_raw in instance_item.keys():
                    res_name = _label(res_name_raw)
                    if res_name and not snake_re.match(res_name):
                        issues.append(CurationValidationIssue(
                            severity="warning",
                            file=fname,
                            rule="naming_convention",
                            message=f"Resource '{res_type}.{res_name}' should use snake_case.",
                            suggestion="Use only lowercase letters, digits, and underscores.",
                        ))

    for output_block in parsed.get("output", []):
        if not isinstance(output_block, dict):
            continue
        for out_name_raw in output_block.keys():
            out_name = _label(out_name_raw)
            if "-" in out_name:
                issues.append(CurationValidationIssue(
                    severity="error",
                    file=fname,
                    rule="naming_convention",
                    message=f"Output '{out_name}' must not contain hyphens.",
                    suggestion="Replace hyphens with underscores.",
                ))

    return issues


def _run_structural_validators(out_dir: Path) -> list[CurationValidationIssue]:
    """
    Run structural checks against the .tf files in out_dir.

    Implements naming and type checks directly using the hcl2 v4 API so that
    results are reliable regardless of the ga_validators compatibility status.
    """
    issues: list[CurationValidationIssue] = []
    for tf_path in sorted(out_dir.glob("*.tf")):
        content = tf_path.read_text(encoding="utf-8")
        try:
            parsed = hcl2.load(StringIO(content))
            issues.extend(_validate_variable_types(tf_path.name, parsed))
            issues.extend(_validate_naming(tf_path.name, parsed))
        except Exception:
            pass  # Syntax errors already reported by Layer 1
    return issues


# ── Layer 3: Provider schema ───────────────────────────────────────────────────

def _extract_hcl_resources(files: dict[str, str]) -> dict[str, dict[str, list[str]]]:
    """
    Parse generated HCL to find {resource_type: {resource_name: [simple_attribute_names]}}.

    Handles hcl2 v4 format where block labels include surrounding quotes and
    block bodies may be wrapped in lists.
    """
    resources: dict[str, dict[str, list[str]]] = {}
    for fname, content in files.items():
        if not fname.endswith(".tf"):
            continue
        try:
            parsed = hcl2.load(StringIO(content))
            for resource_block in parsed.get("resource", []):
                if not isinstance(resource_block, dict):
                    continue
                for res_type_raw, res_val in resource_block.items():
                    res_type = _label(res_type_raw)
                    resources.setdefault(res_type, {})
                    for instance_item in _iter_block_attrs(res_val):
                        for res_name_raw, res_attrs_val in instance_item.items():
                            res_name = _label(res_name_raw)
                            attrs: dict = _flat_attrs(res_attrs_val)
                            simple = [
                                k for k, v in attrs.items()
                                if k not in _META_ARGS and not isinstance(v, (dict, list))
                            ]
                            resources[res_type].setdefault(res_name, []).extend(simple)
        except Exception:
            pass
    return resources


async def _validate_provider_schema(
    files: dict[str, str],
    provider: str,
) -> tuple[list[CurationValidationIssue], bool]:
    """
    Validate resource types and attribute names against the live provider schema.
    Returns (issues, schema_was_checked).
    """
    schema = await fetch_provider_schema(provider)
    if schema is None:
        return [], False

    resources = _extract_hcl_resources(files)
    if not resources:
        return [], True

    issues: list[CurationValidationIssue] = []
    for res_type, instances in resources.items():
        if not any(res_type.startswith(p) for p in _PROVIDER_PREFIXES):
            continue

        if not check_resource_type(schema, res_type):
            issues.append(CurationValidationIssue(
                severity="error",
                file="main.tf",
                rule="unknown_resource_type",
                message=f"Resource type '{res_type}' not found in the {provider} provider schema.",
                suggestion=(
                    f"Verify the resource type at "
                    f"https://registry.terraform.io/providers/hashicorp/{provider}/latest/docs"
                ),
            ))
            continue

        for _res_name, attrs in instances.items():
            for attr in attrs:
                if not check_attribute(schema, res_type, attr):
                    issues.append(CurationValidationIssue(
                        severity="warning",
                        file="main.tf",
                        rule="unknown_attribute",
                        message=f"Attribute '{attr}' not found in schema for '{res_type}'.",
                        suggestion=(
                            f"Check provider documentation for '{res_type}' — "
                            f"the attribute may be renamed or live inside a nested block."
                        ),
                    ))

    return issues, True


# ── Layer 4: Terraform CLI ────────────────────────────────────────────────────

def _terraform_available() -> bool:
    try:
        r = subprocess.run(["terraform", "version"], capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _terraform_cli_validate_sync(files: dict[str, str]) -> tuple[bool, list[CurationValidationIssue]]:
    """Synchronous inner function — run via asyncio.to_thread to avoid blocking the event loop."""
    issues: list[CurationValidationIssue] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        for fname, content in files.items():
            (tmp / fname).write_text(content, encoding="utf-8")

        # terraform init
        init = subprocess.run(
            ["terraform", "init", "-backend=false", "-no-color"],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if init.returncode != 0:
            stderr = (init.stderr or init.stdout or "").strip()
            return False, [CurationValidationIssue(
                severity="error",
                file="",
                rule="terraform_init",
                message=f"terraform init failed: {stderr[:500]}",
                suggestion="Check provider version constraints in versions.tf.",
            )]

        # terraform validate -json
        validate = subprocess.run(
            ["terraform", "validate", "-json", "-no-color"],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=30,
        )

        try:
            data = json.loads(validate.stdout or "{}")
        except Exception:
            return validate.returncode == 0, []

        passed = bool(data.get("valid", validate.returncode == 0))

        for diag in data.get("diagnostics", []):
            sev = "error" if diag.get("severity") == "error" else "warning"
            pos = diag.get("range", {})
            diag_file = ""
            diag_line: Optional[int] = None
            if pos:
                diag_file = Path(pos.get("filename", "")).name
                diag_line = pos.get("start", {}).get("line")
            summary = diag.get("summary", "")
            detail = diag.get("detail", "")
            msg = f"{summary}: {detail}".strip(": ") if detail else summary
            issues.append(CurationValidationIssue(
                severity=sev,
                file=diag_file,
                line=diag_line,
                rule="terraform_validate",
                message=msg,
            ))

    return passed, issues


async def _terraform_cli_validate(
    files: dict[str, str],
) -> tuple[bool, list[CurationValidationIssue]]:
    """Run terraform init + validate in a thread pool to avoid blocking the async event loop."""
    return await asyncio.to_thread(_terraform_cli_validate_sync, files)


# ── Public entry point ────────────────────────────────────────────────────────

async def validate_curation(
    files: dict[str, str],
    out_dir: Path,
    session: CurationSession,
) -> CurationValidationResult:
    """
    Run all four validation layers against the generated Terraform files.

    Layers 1 and 2 always run (offline-safe).
    Layer 3 requires network access (skipped if offline).
    Layer 4 requires Terraform CLI (skipped if not in PATH).

    Never raises — all errors become CurationValidationIssues.
    """
    all_issues: list[CurationValidationIssue] = []

    print("[validator] Layer 1: HCL syntax check")
    all_issues.extend(_validate_hcl_syntax(files))

    print("[validator] Layer 1b: Security heuristics")
    all_issues.extend(_validate_security(files))

    print("[validator] Layer 1c: Inline flag extraction")
    all_issues.extend(_extract_inline_flags(files))

    print("[validator] Layer 2: Structural validation")
    all_issues.extend(_run_structural_validators(out_dir))

    print("[validator] Layer 3: Provider schema validation")
    provider_schema_checked = False
    try:
        schema_issues, checked = await _validate_provider_schema(files, session.provider.value)
        all_issues.extend(schema_issues)
        provider_schema_checked = checked
    except Exception as exc:
        print(f"[validator] Provider schema check failed: {exc}")

    print("[validator] Layer 4: Terraform CLI validation")
    cli_available = _terraform_available()
    cli_passed: Optional[bool] = None
    if cli_available:
        try:
            cli_passed, cli_issues = await _terraform_cli_validate(files)
            all_issues.extend(cli_issues)
        except Exception as exc:
            print(f"[validator] Terraform CLI validation failed: {exc}")
            cli_available = False

    error_count = sum(1 for i in all_issues if i.severity == "error")
    warning_count = sum(1 for i in all_issues if i.severity == "warning")
    overall_passed = error_count == 0 and (cli_passed is None or cli_passed)

    print(
        f"[validator] Done — passed={overall_passed}, "
        f"errors={error_count}, warnings={warning_count}"
    )
    return CurationValidationResult(
        passed=overall_passed,
        error_count=error_count,
        warning_count=warning_count,
        issues=all_issues,
        terraform_cli_available=cli_available,
        terraform_validate_passed=cli_passed,
        provider_schema_checked=provider_schema_checked,
    )
