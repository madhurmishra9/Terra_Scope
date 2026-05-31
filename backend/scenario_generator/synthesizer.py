"""
synthesizer.py — Deterministically assemble GeneratedScenario files from a
ModuleSpec + ScenarioEntry.

LLM is only used to fill realistic example values for required string variables
that have no obvious value (provider-aware). Everything structural (file layout,
module call, tfvars format) is pure Python.

No variables absent from ModuleSpec are ever invented.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.scenario_generator.models import (
    GeneratedScenario,
    ModuleSpec,
    ScenarioEntry,
    VariableSpec,
)

_PROJECT_ROOT = Path(__file__).parent.parent.parent


# ── Public API ────────────────────────────────────────────────────────────────

def synthesize_scenario(
    scenario: ScenarioEntry,
    spec: ModuleSpec,
    module_relative_path: str = "../../../../module",
) -> GeneratedScenario:
    """Build all files for one scenario without calling the LLM.

    Returns a GeneratedScenario with files dict populated but output_dir empty
    (the caller writes to disk and sets output_dir).
    """
    files: dict[str, str] = {}

    # main.tf — root config that calls the module
    files["main.tf"] = _render_root_main(scenario, spec, module_relative_path)

    # terraform.tfvars — variable values for this scenario
    files["terraform.tfvars"] = _render_tfvars(scenario, spec)

    # versions.tf — provider + terraform version pins
    files["versions.tf"] = _render_versions(spec)

    return GeneratedScenario(
        name=scenario.name,
        based_on_scenario=scenario,
        files=files,
    )


def write_scenario(
    gs: GeneratedScenario,
    base_output_dir: Path,
    module_source_path: str,
) -> GeneratedScenario:
    """Write all scenario files to disk; return updated GeneratedScenario with output_dir set."""
    scenario_dir = base_output_dir / gs.name
    scenario_dir.mkdir(parents=True, exist_ok=True)

    for rel_path, content in gs.files.items():
        dest = scenario_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")

    return gs.model_copy(update={"output_dir": str(scenario_dir)})


def build_output_dir(module_name: str) -> Path:
    slug = re.sub(r"[^a-z0-9_-]", "_", module_name.lower()).strip("_") or "module"
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out = _PROJECT_ROOT / "output" / f"{slug}_scenarios_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    return out


# ── File renderers ────────────────────────────────────────────────────────────

def _render_root_main(
    scenario: ScenarioEntry,
    spec: ModuleSpec,
    module_source: str,
) -> str:
    """Render the root main.tf that calls the module under test."""
    lines: list[str] = [
        f'# Scenario: {scenario.name}',
        f'# {scenario.description}',
        '',
        f'module "{_slug(spec.module_name)}" {{',
        f'  source = "{module_source}"',
        '',
    ]

    # Set each variable that appears in the scenario's var_values
    for var_spec in spec.variables:
        if var_spec.name in scenario.var_values:
            val = scenario.var_values[var_spec.name]
            lines.append(f'  {var_spec.name:<30} = {_hcl_value(val, var_spec)}')
        elif var_spec.required:
            # Required var not in var_values — use a safe placeholder
            placeholder = _placeholder_value(var_spec)
            lines.append(f'  {var_spec.name:<30} = {placeholder}  # REQUIRED — set a real value')

    lines += ['', '}', '']
    return '\n'.join(lines)


def _render_tfvars(scenario: ScenarioEntry, spec: ModuleSpec) -> str:
    """Render a terraform.tfvars with the scenario's variable values."""
    lines: list[str] = [
        f'# terraform.tfvars — scenario: {scenario.name}',
        f'# {scenario.description}',
        '',
    ]
    for var_spec in spec.variables:
        if var_spec.name in scenario.var_values:
            val = scenario.var_values[var_spec.name]
            desc = var_spec.description[:60].rstrip() if var_spec.description else var_spec.type
            lines.append(f'{var_spec.name} = {_hcl_value(val, var_spec)}  # {desc}')
    return '\n'.join(lines) + '\n'


def _render_versions(spec: ModuleSpec) -> str:
    """Render a minimal versions.tf with the detected provider pin."""
    provider = spec.provider or "google"
    pins = {"google": "~> 5.40", "aws": "~> 5.70", "azurerm": "~> 3.110"}
    pin = pins.get(provider, ">= 1.0")
    tf_ver = spec.terraform_required_version or ">= 1.5.0"
    return (
        'terraform {\n'
        f'  required_version = "{tf_ver}"\n'
        '  required_providers {\n'
        f'    {provider} = {{\n'
        f'      source  = "hashicorp/{provider}"\n'
        f'      version = "{pin}"\n'
        '    }\n'
        '  }\n'
        '}\n'
    )


# ── HCL value serialisation ───────────────────────────────────────────────────

def _hcl_value(val: Any, var_spec: VariableSpec) -> str:
    """Serialize a Python value to an HCL literal."""
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        return f'"{_escape_hcl_str(val)}"'
    if isinstance(val, list):
        if not val:
            return "[]"
        items = ", ".join(_hcl_value(v, var_spec) for v in val)
        return f"[{items}]"
    if isinstance(val, dict):
        if not val:
            return "{}"
        pairs = "\n".join(
            f'    {k} = {_hcl_value(v, var_spec)}'
            for k, v in val.items()
        )
        return f"{{\n{pairs}\n  }}"
    return f'"{val}"'


def _escape_hcl_str(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("${", "$${")


def _placeholder_value(var_spec: VariableSpec) -> str:
    """Return a safe HCL literal placeholder for a required variable."""
    t = var_spec.type.lower()
    if t == "bool":
        return "false"
    if t in ("number", "int"):
        return "1"
    if "list" in t or "set" in t:
        return "[]"
    if "map" in t:
        return "{}"
    if var_spec.enum_values:
        return f'"{var_spec.enum_values[0]}"'
    return '"REPLACE_ME"'


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_") or "module"
