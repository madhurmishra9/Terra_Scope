"""
hcl_tools.py — Parses Terraform HCL files into structured Python objects.
Uses python-hcl2 for real AST parsing (not regex). This is what gives
the agent actual CODE UNDERSTANDING, not just text matching.
"""
from __future__ import annotations

import re
from typing import Optional, Any
import hcl2
from io import StringIO

from backend.agent.models import VariableInfo, ResourceInfo
from backend.agent.tools.git_tools import (
    get_file_at_tag, list_tf_files_at_tag, get_latest_tag
)


# ── Core HCL parsing ──────────────────────────────────────────────────────────

def parse_hcl_content(content: str) -> dict:
    """Parse HCL string into a Python dict. Returns {} on error."""
    try:
        return hcl2.load(StringIO(content))
    except Exception as e:
        print(f"[hcl] Parse error: {e}")
        return {}


def get_all_variables(repo_name: str, tag: str) -> list[VariableInfo]:
    """
    Extract all input variables from variables.tf at a specific tag.
    Returns structured VariableInfo objects with types, defaults, required status.
    """
    tag = tag or get_latest_tag(repo_name) or "main"
    results: list[VariableInfo] = []

    # Try variables.tf first, then scan all .tf files
    candidates = ["variables.tf"]
    all_tf = list_tf_files_at_tag(repo_name, tag)
    candidates += [f for f in all_tf if f != "variables.tf"]

    for file_path in candidates:
        content = get_file_at_tag(repo_name, tag, file_path)
        if not content:
            continue
        parsed = parse_hcl_content(content)
        raw_vars = parsed.get("variable", {})
        if not raw_vars:
            continue

        lines = content.splitlines()
        for var_name, var_body in raw_vars.items():
            if isinstance(var_body, list):
                var_body = var_body[0] if var_body else {}

            # Find the line number of this variable declaration
            line_num = _find_block_line(lines, "variable", var_name)

            results.append(VariableInfo(
                name=var_name,
                type=_type_to_str(var_body.get("type", "any")),
                description=str(var_body.get("description", "")),
                default=_safe_str(var_body.get("default")),
                required="default" not in var_body,
                file_path=file_path,
                line=line_num,
            ))

    return results


def get_all_resources(repo_name: str, tag: str) -> list[ResourceInfo]:
    """
    Extract all resource blocks from all .tf files at a specific tag.
    Focuses on google_* resources for GCP context.
    """
    tag = tag or get_latest_tag(repo_name) or "main"
    results: list[ResourceInfo] = []

    all_tf = list_tf_files_at_tag(repo_name, tag)
    for file_path in all_tf:
        content = get_file_at_tag(repo_name, tag, file_path)
        if not content:
            continue
        parsed = parse_hcl_content(content)
        raw_resources = parsed.get("resource", {})
        if not raw_resources:
            continue

        lines = content.splitlines()
        for res_type, instances in raw_resources.items():
            if not isinstance(instances, dict):
                continue
            for res_name, res_body in instances.items():
                if isinstance(res_body, list):
                    res_body = res_body[0] if res_body else {}

                line_start = _find_block_line(lines, "resource", res_type, res_name)
                # Extract key attributes (avoid dumping entire body)
                attrs = _extract_key_attributes(res_body)

                results.append(ResourceInfo(
                    resource_type=res_type,
                    resource_name=res_name,
                    file_path=file_path,
                    line_start=line_start,
                    attributes=attrs,
                ))

    return results


def get_outputs(repo_name: str, tag: str) -> list[dict]:
    """Extract all output values from outputs.tf."""
    tag = tag or get_latest_tag(repo_name) or "main"
    results = []
    for file_path in ["outputs.tf"] + list_tf_files_at_tag(repo_name, tag):
        content = get_file_at_tag(repo_name, tag, file_path)
        if not content:
            continue
        parsed = parse_hcl_content(content)
        for out_name, out_body in parsed.get("output", {}).items():
            if isinstance(out_body, list):
                out_body = out_body[0] if out_body else {}
            results.append({
                "name": out_name,
                "description": out_body.get("description", ""),
                "sensitive": out_body.get("sensitive", False),
                "file_path": file_path,
            })
    return results


def get_provider_requirements(repo_name: str, tag: str) -> dict:
    """
    Extract required_providers and terraform version constraints.
    Critical for diagnosing provider version conflicts.
    """
    tag = tag or get_latest_tag(repo_name) or "main"
    for file_path in ["versions.tf", "main.tf"] + list_tf_files_at_tag(repo_name, tag):
        content = get_file_at_tag(repo_name, tag, file_path)
        if not content:
            continue
        parsed = parse_hcl_content(content)
        terraform_blocks = parsed.get("terraform", [])
        if not terraform_blocks:
            continue
        if isinstance(terraform_blocks, list):
            terraform_blocks = terraform_blocks[0] if terraform_blocks else {}
        return {
            "required_version": terraform_blocks.get("required_version", "not specified"),
            "required_providers": terraform_blocks.get("required_providers", {}),
            "source_file": file_path,
        }
    return {}


def get_iam_bindings(repo_name: str, tag: str) -> list[dict]:
    """
    Extract all IAM-related resources — critical for security questions.
    Covers: _iam_binding, _iam_member, _iam_policy resource types.
    """
    resources = get_all_resources(repo_name, tag)
    iam_resources = [
        r for r in resources
        if any(kw in r.resource_type for kw in ["_iam_", "_policy", "_binding", "_member"])
    ]
    return [
        {
            "type": r.resource_type,
            "name": r.resource_name,
            "file": r.file_path,
            "line": r.line_start,
            "role": r.attributes.get("role"),
            "member": r.attributes.get("member"),
        }
        for r in iam_resources
    ]


def summarize_module(repo_name: str, tag: str) -> dict:
    """
    High-level code understanding summary of the module at a tag.
    Used as initial context for any query.
    """
    tag = tag or get_latest_tag(repo_name) or "main"
    variables = get_all_variables(repo_name, tag)
    resources  = get_all_resources(repo_name, tag)
    providers  = get_provider_requirements(repo_name, tag)
    outputs    = get_outputs(repo_name, tag)

    resource_types = list({r.resource_type for r in resources})
    google_resources = [rt for rt in resource_types if rt.startswith("google_")]

    return {
        "tag": tag,
        "required_variables": [v.name for v in variables if v.required],
        "optional_variables": [v.name for v in variables if not v.required],
        "total_variables": len(variables),
        "google_resource_types": google_resources,
        "total_resources": len(resources),
        "outputs": [o["name"] for o in outputs],
        "provider_requirements": providers,
        "iam_resources": [r.resource_type for r in resources
                          if "_iam_" in r.resource_type or "_policy" in r.resource_type],
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_block_line(lines: list[str], block_type: str, *args) -> int:
    """Find the line number of an HCL block declaration."""
    for i, line in enumerate(lines, 1):
        if block_type in line and all(f'"{a}"' in line or a in line for a in args):
            return i
    return 0


def _type_to_str(t: Any) -> str:
    if isinstance(t, str):
        return t
    if isinstance(t, dict):
        return str(t)
    return "any"


def _safe_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    return str(v)[:200]


def _extract_key_attributes(body: dict) -> dict:
    """Extract important attributes, skip large nested blocks."""
    SKIP_KEYS = {"lifecycle", "timeouts", "depends_on"}
    result = {}
    for k, v in body.items():
        if k in SKIP_KEYS:
            continue
        if isinstance(v, (str, int, float, bool)):
            result[k] = v
        elif isinstance(v, list) and len(v) > 0 and isinstance(v[0], (str, int)):
            result[k] = v
        elif isinstance(v, dict) and len(v) < 5:
            result[k] = {ik: iv for ik, iv in v.items()
                         if isinstance(iv, (str, int, float, bool))}
    return result
