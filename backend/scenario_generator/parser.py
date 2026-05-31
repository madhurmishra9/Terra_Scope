"""
parser.py — Deterministic AST-based ModuleSpec builder.

Reuses hcl_tools.parse_hcl_content as the parsing primitive.
All logic here is pure Python / regex — NO LLM involvement.

Feature-gate detection rules:
  - count = var.X ? 1 : 0          → GateType.COUNT  (gate var = X)
  - count = var.X                   → GateType.COUNT
  - for_each = var.X                → GateType.FOR_EACH
  - for_each = toset(var.X)         → GateType.FOR_EACH
  - dynamic "label" { for_each = var.X ... } → GateType.DYNAMIC
  - Expressions involving var.X != null, var.X == true
"""
from __future__ import annotations

import re
from io import StringIO
from typing import Any, Optional

import hcl2

from backend.scenario_generator.models import (
    DataSourceSpec,
    FeatureGate,
    GateType,
    ModuleCallSpec,
    ModuleSpec,
    OutputSpec,
    ResourceSpec,
    ValidationRule,
    VariableSpec,
)

# ── Regex patterns for gate detection ────────────────────────────────────────

# Matches: count = var.X ? 1 : 0  OR  count = var.X
_COUNT_VAR_RE = re.compile(
    r'\bcount\s*=\s*(?:var\.(\w+)\s*\?\s*\d+\s*:\s*\d+|var\.(\w+))',
    re.IGNORECASE,
)

# Matches: for_each = var.X  OR  for_each = toset(var.X)  OR  for_each = var.X != null ? ...
_FOR_EACH_VAR_RE = re.compile(
    r'\bfor_each\s*=\s*(?:toset\s*\(\s*)?var\.(\w+)',
    re.IGNORECASE,
)

# Matches dynamic block: dynamic "label" { ... for_each = var.X ... }
_DYNAMIC_BLOCK_RE = re.compile(
    r'\bdynamic\s+"(\w+)"\s*\{([^}]*(?:\{[^}]*\}[^}]*)*)\}',
    re.DOTALL,
)
_DYNAMIC_FOR_EACH_VAR_RE = re.compile(r'\bfor_each\s*=\s*(?:var\.(\w+)|[^\n]*var\.(\w+))')

# Enum extraction from validation: contains(["a","b",...], var.X)
_ENUM_RE = re.compile(r'contains\s*\(\s*\[([^\]]+)\]\s*,\s*var\.\w+\s*\)')


# ── Public API ────────────────────────────────────────────────────────────────

def build_module_spec(
    tf_files: dict[str, str],
    module_name: str = "module",
    module_source: str = "",
) -> ModuleSpec:
    """Build a complete ModuleSpec from a dict of {filename: hcl_content}.

    This is the sole public function.  Everything is deterministic — no LLM.
    """
    variables  = _parse_variables(tf_files)
    outputs    = _parse_outputs(tf_files)
    resources, feature_gates = _parse_resources_and_gates(tf_files)
    data_srcs  = _parse_data_sources(tf_files)
    mod_calls  = _parse_module_calls(tf_files)
    provider   = _detect_provider(tf_files)
    tf_ver     = _detect_tf_version(tf_files)

    return ModuleSpec(
        module_name=module_name,
        module_source=module_source,
        provider=provider,
        terraform_required_version=tf_ver,
        variables=variables,
        outputs=outputs,
        resources=resources,
        data_sources=data_srcs,
        module_calls=mod_calls,
        feature_gates=feature_gates,
    )


# ── Variable parsing ──────────────────────────────────────────────────────────

def _parse_variables(tf_files: dict[str, str]) -> list[VariableSpec]:
    specs: list[VariableSpec] = []
    seen: set[str] = set()

    priority = ["variables.tf"] + [k for k in tf_files if k != "variables.tf"]
    for fname in priority:
        content = tf_files.get(fname, "")
        if not content:
            continue
        parsed = _safe_parse(content)
        raw_var_list = parsed.get("variable", [])
        if not raw_var_list:
            continue

        for var_entry in _normalise_block_list(raw_var_list):
            for var_name_raw, var_body in var_entry.items():
                var_name = var_name_raw.strip('"')
                if var_name in seen:
                    continue
                seen.add(var_name)
            if isinstance(var_body, list):
                var_body = var_body[0] if var_body else {}

            has_default = "default" in var_body
            validation_rules = _extract_validation_rules(var_body)
            enum_values      = _extract_enum_values(content, var_name)
            raw_default = var_body.get("default") if has_default else None
            default_val = _unquote(raw_default) if has_default else None

            specs.append(VariableSpec(
                name=var_name,
                type=_type_to_str(var_body.get("type", "string")),
                default=default_val,
                required=not has_default,
                nullable=var_body.get("nullable", True),
                sensitive=bool(var_body.get("sensitive", False)),
                description=str(var_body.get("description", "")),
                validation_rules=validation_rules,
                enum_values=enum_values,
            ))

    return specs


def _extract_validation_rules(var_body: dict) -> list[ValidationRule]:
    rules: list[ValidationRule] = []
    validation = var_body.get("validation", [])
    if isinstance(validation, dict):
        validation = [validation]
    for rule in validation:
        if isinstance(rule, dict):
            cond = str(rule.get("condition", ""))
            msg  = str(rule.get("error_message", ""))
            if cond:
                rules.append(ValidationRule(condition_expr=cond, error_message=msg))
    return rules


def _extract_enum_values(content: str, var_name: str) -> list[str]:
    """Try to extract explicit enum values from validation blocks."""
    values: list[str] = []
    for m in _ENUM_RE.finditer(content):
        raw_list = m.group(1)
        values = [v.strip().strip('"\'') for v in raw_list.split(",") if v.strip()]
        if values:
            break
    return values


# ── Output parsing ────────────────────────────────────────────────────────────

def _parse_outputs(tf_files: dict[str, str]) -> list[OutputSpec]:
    specs: list[OutputSpec] = []
    seen: set[str] = set()

    priority = ["outputs.tf"] + [k for k in tf_files if k != "outputs.tf"]
    for fname in priority:
        content = tf_files.get(fname, "")
        if not content:
            continue
        parsed = _safe_parse(content)
        raw_out_list = parsed.get("output", [])
        for out_entry in _normalise_block_list(raw_out_list):
            for out_name_raw, out_body in out_entry.items():
                out_name = out_name_raw.strip('"')
                if out_name in seen:
                    continue
                seen.add(out_name)
                if isinstance(out_body, list):
                    out_body = out_body[0] if out_body else {}
                specs.append(OutputSpec(
                    name=out_name,
                    description=str(out_body.get("description", "")),
                    sensitive=bool(out_body.get("sensitive", False)),
                    value_expr=str(out_body.get("value", "")),
                ))

    return specs


# ── Resource + feature-gate parsing ──────────────────────────────────────────

def _parse_resources_and_gates(
    tf_files: dict[str, str],
) -> tuple[list[ResourceSpec], list[FeatureGate]]:
    resources: list[ResourceSpec] = []
    gates_by_var: dict[str, FeatureGate] = {}

    for fname, content in tf_files.items():
        if not fname.endswith(".tf"):
            continue
        parsed = _safe_parse(content)

        for res_entry in _normalise_block_list(parsed.get("resource", [])):
            for res_type_raw, inner in res_entry.items():
                res_type = res_type_raw.strip('"')
                instances = inner if isinstance(inner, dict) else {}
                for res_name_raw, res_body in instances.items():
                    res_name = res_name_raw.strip('"')
                    if isinstance(res_body, list):
                        res_body = res_body[0] if res_body else {}

                    # Detect feature gate for this resource
                    gate_var = _detect_gate_variable(content, res_type, res_name, res_body)

                    resources.append(ResourceSpec(
                        resource_type=res_type,
                        resource_name=res_name,
                        file_path=fname,
                        gated_by=gate_var,
                    ))

                    # Accumulate gate info
                    if gate_var:
                        gate_type = _detect_gate_type(content, res_type, res_name, res_body)
                        rid = f"{res_type}.{res_name}"
                        if gate_var not in gates_by_var:
                            gates_by_var[gate_var] = FeatureGate(
                                gate_variable=gate_var,
                                gate_type=gate_type,
                                gated_resources=[rid],
                                on_when=_describe_gate_condition(gate_var, gate_type),
                            )
                        else:
                            if rid not in gates_by_var[gate_var].gated_resources:
                                gates_by_var[gate_var].gated_resources.append(rid)

        # Dynamic blocks — scan raw text
        for dm in _DYNAMIC_BLOCK_RE.finditer(content):
            block_label = dm.group(1)
            block_body  = dm.group(2)
            m = _DYNAMIC_FOR_EACH_VAR_RE.search(block_body)
            if m:
                gate_var = m.group(1) or m.group(2)
                if gate_var:
                    if gate_var not in gates_by_var:
                        gates_by_var[gate_var] = FeatureGate(
                            gate_variable=gate_var,
                            gate_type=GateType.DYNAMIC,
                            gated_resources=[],
                            gated_blocks=[block_label],
                            on_when=_describe_gate_condition(gate_var, GateType.DYNAMIC),
                        )
                    else:
                        if block_label not in gates_by_var[gate_var].gated_blocks:
                            gates_by_var[gate_var].gated_blocks.append(block_label)

    return resources, list(gates_by_var.values())


def _detect_gate_variable(
    content: str, res_type: str, res_name: str, res_body: dict
) -> Optional[str]:
    """Return the controlling variable name if this resource has a count or for_each gate."""
    # Check count in parsed body
    count_val = res_body.get("count")
    if count_val is not None:
        gate = _extract_var_from_expr(str(count_val))
        if gate:
            return gate

    # Check for_each in parsed body
    fe_val = res_body.get("for_each")
    if fe_val is not None:
        gate = _extract_var_from_expr(str(fe_val))
        if gate:
            return gate

    # Fall back to raw text scan for this resource block
    block_re = re.compile(
        rf'resource\s+"[^"]*{re.escape(res_type)}[^"]*"\s+"[^"]*{re.escape(res_name)}[^"]*"\s*\{{([^}}]{{1,2000}})\}}',
        re.DOTALL,
    )
    m = block_re.search(content)
    if m:
        block_text = m.group(1)
        for pat in (_COUNT_VAR_RE, _FOR_EACH_VAR_RE):
            fm = pat.search(block_text)
            if fm:
                return next(g for g in fm.groups() if g)
    return None


def _detect_gate_type(
    content: str, res_type: str, res_name: str, res_body: dict
) -> GateType:
    count_val = res_body.get("count")
    if count_val is not None:
        return GateType.COUNT
    fe_val = res_body.get("for_each")
    if fe_val is not None:
        return GateType.FOR_EACH

    block_re = re.compile(
        rf'resource\s+"[^"]*{re.escape(res_type)}[^"]*"\s+"[^"]*{re.escape(res_name)}[^"]*"\s*\{{([^}}]{{1,2000}})\}}',
        re.DOTALL,
    )
    m = block_re.search(content)
    if m:
        block_text = m.group(1)
        if _COUNT_VAR_RE.search(block_text):
            return GateType.COUNT
        if _FOR_EACH_VAR_RE.search(block_text):
            return GateType.FOR_EACH
    return GateType.COUNT


def _extract_var_from_expr(expr: str) -> Optional[str]:
    """Pull the first var.X name from an expression string."""
    m = re.search(r'\bvar\.(\w+)', expr)
    return m.group(1) if m else None


def _describe_gate_condition(gate_var: str, gate_type: GateType) -> str:
    if gate_type == GateType.COUNT:
        return f"var.{gate_var} == true  OR  var.{gate_var} != null"
    if gate_type == GateType.FOR_EACH:
        return f"length(var.{gate_var}) > 0  OR  var.{gate_var} != null"
    return f"var.{gate_var} != null"


# ── Data source parsing ───────────────────────────────────────────────────────

def _parse_data_sources(tf_files: dict[str, str]) -> list[DataSourceSpec]:
    specs: list[DataSourceSpec] = []
    for fname, content in tf_files.items():
        if not fname.endswith(".tf"):
            continue
        parsed = _safe_parse(content)
        for data_entry in _normalise_block_list(parsed.get("data", [])):
            for data_type_raw, inner in data_entry.items():
                data_type = data_type_raw.strip('"')
                instances = inner if isinstance(inner, dict) else {}
                for data_name_raw in instances:
                    specs.append(DataSourceSpec(
                        data_type=data_type,
                        data_name=data_name_raw.strip('"'),
                        file_path=fname,
                    ))
    return specs


# ── Module call parsing ───────────────────────────────────────────────────────

def _parse_module_calls(tf_files: dict[str, str]) -> list[ModuleCallSpec]:
    specs: list[ModuleCallSpec] = []
    for fname, content in tf_files.items():
        if not fname.endswith(".tf"):
            continue
        parsed = _safe_parse(content)
        for mod_entry in _normalise_block_list(parsed.get("module", [])):
            for mod_label_raw, mod_body in mod_entry.items():
                mod_label = mod_label_raw.strip('"')
                if isinstance(mod_body, list):
                    mod_body = mod_body[0] if mod_body else {}
                source = str(mod_body.get("source", ""))
                reserved = {"source", "version", "depends_on", "count", "for_each", "providers"}
                req_inputs = [k.strip('"') for k in mod_body if k.strip('"') not in reserved]
                specs.append(ModuleCallSpec(
                    module_name=mod_label,
                    source=source,
                    required_inputs=req_inputs,
                ))
    return specs


# ── Provider / version detection ──────────────────────────────────────────────

def _detect_provider(tf_files: dict[str, str]) -> str:
    """Heuristic: infer primary provider from resource types or versions.tf."""
    for fname in ["versions.tf", "main.tf"] + list(tf_files.keys()):
        content = tf_files.get(fname, "")
        if not content:
            continue
        parsed = _safe_parse(content)
        for tf_block in _iter_blocks(parsed.get("terraform", [])):
            rp = tf_block.get("required_providers", [])
            for prov_entry in _normalise_block_list(rp):
                if "google" in prov_entry:
                    return "google"
                if "aws" in prov_entry:
                    return "aws"
                if "azurerm" in prov_entry:
                    return "azurerm"

    # Fall back to resource type prefix
    for content in tf_files.values():
        parsed = _safe_parse(content)
        for res_entry in _normalise_block_list(parsed.get("resource", [])):
            for res_type_raw in res_entry:
                res_type = res_type_raw.strip('"')
                if res_type.startswith("google_"):
                    return "google"
                if res_type.startswith("aws_"):
                    return "aws"
                if res_type.startswith("azurerm_"):
                    return "azurerm"
    return "unknown"


def _detect_tf_version(tf_files: dict[str, str]) -> str:
    for fname in ["versions.tf", "main.tf"] + list(tf_files.keys()):
        content = tf_files.get(fname, "")
        if not content:
            continue
        parsed = _safe_parse(content)
        for tf_block in _iter_blocks(parsed.get("terraform", [])):
            ver = tf_block.get("required_version", "")
            if ver:
                return str(ver).strip('"')
    return ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_parse(content: str) -> dict:
    try:
        return hcl2.load(StringIO(content))
    except Exception:
        return {}


def _normalise_block_list(raw: Any) -> list[dict]:
    """Normalise the list-of-single-key-dicts structure python-hcl2 produces.

    python-hcl2 returns labelled blocks as:
        [{"\"label1\"": {...body...}}, {"\"label2\"": {...}}]

    This function returns that list as-is when it matches, or wraps a plain
    dict, or returns [] for anything else.
    """
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        return [raw]
    return []


def _type_to_str(t: Any) -> str:
    if isinstance(t, str):
        return t
    if isinstance(t, dict):
        return str(t)
    return "string"


def _iter_blocks(blocks: Any) -> list[dict]:
    if isinstance(blocks, list):
        return [b for b in blocks if isinstance(b, dict)]
    if isinstance(blocks, dict):
        return [blocks]
    return []


def _unquote(val: Any) -> Any:
    """Strip surrounding double-quotes that python-hcl2 adds to string literals."""
    if isinstance(val, str) and len(val) >= 2 and val.startswith('"') and val.endswith('"'):
        return val[1:-1]
    return val
