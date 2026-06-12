"""
planner.py — PydanticAI Agent that produces a ScenarioPlan from a ModuleSpec.

The agent only picks VALUES and NAMES; the gate/variable list comes from the
already-deterministically-built ModuleSpec. A repair loop handles gemma4:12b's
tendency to emit non-JSON or truncated output.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from backend.config import get_config
from backend.scenario_generator.models import (
    ModuleSpec,
    ScenarioEntry,
    ScenarioPlan,
)

_planner_agent: Optional[Agent] = None

_SYSTEM_PROMPT = """
You are a Terraform test-scenario planner.
Given a ModuleSpec JSON, produce a ScenarioPlan JSON covering:
  1. minimal      — only required variables set
  2. maximal      — all optional variables also set
  3. gate_<var>_on  — for every feature gate: set the gating variable to enable it
  4. gate_<var>_off — for every feature gate: set it to disable (or leave unset)
  5. boundary     — for vars with validation/enum: one valid edge value + one invalid (expect_failure=true)
  6. foreach_zero — for for_each gates: set the var to [] or {} so count = 0
  7. foreach_many — for for_each gates: set the var to a list with 2-3 entries

Rules:
- Choose realistic, provider-appropriate values (e.g. GCP region = "us-central1").
- Only use variables listed in the ModuleSpec — never invent new ones.
- For required variables, always provide a valid value.
- features_expected should list resource type.name for resources expected in the plan.
- Output ONLY a valid JSON object matching the ScenarioPlan schema. No markdown.
"""


def _make_model() -> OpenAIChatModel:
    cfg = get_config()
    return OpenAIChatModel(
        model_name=cfg.llm.model,
        provider=OpenAIProvider(
            base_url=cfg.llm.base_url.rstrip("/") + "/v1",
            api_key="ollama",
        ),
    )


def _get_planner_agent() -> Agent:
    global _planner_agent
    if _planner_agent is None:
        _planner_agent = Agent(
            model=_make_model(),
            output_type=ScenarioPlan,
            system_prompt=_SYSTEM_PROMPT,
        )
    return _planner_agent


def _build_planner_prompt(spec: ModuleSpec) -> str:
    required_vars = [
        {"name": v.name, "type": v.type, "description": v.description}
        for v in spec.required_variables
    ]
    optional_vars = [
        {
            "name": v.name,
            "type": v.type,
            "default": v.default,
            "enum_values": v.enum_values,
            "description": v.description,
        }
        for v in spec.optional_variables
    ]
    gates = [
        {
            "gate_variable": g.gate_variable,
            "gate_type": g.gate_type,
            "gated_resources": g.gated_resources,
            "gated_blocks": g.gated_blocks,
            "on_when": g.on_when,
        }
        for g in spec.feature_gates
    ]
    resources = [f"{r.resource_type}.{r.resource_name}" for r in spec.resources]

    prompt_data = {
        "module_name": spec.module_name,
        "module_source": spec.module_source,
        "provider": spec.provider,
        "required_variables": required_vars,
        "optional_variables": optional_vars,
        "feature_gates": gates,
        "resources": resources,
    }
    return (
        "MODULE SPEC:\n"
        + json.dumps(prompt_data, indent=2)
        + "\n\nProduce the ScenarioPlan JSON now."
    )


def _repair_prompt(spec: ModuleSpec, error: str) -> str:
    return (
        f"The previous ScenarioPlan JSON was invalid: {error}\n\n"
        "Please produce a corrected ScenarioPlan JSON. Ensure:\n"
        "- 'scenarios' is a non-empty JSON array\n"
        "- Each scenario has: name (string), description (string), var_values (object), "
        "features_expected (array), expect_failure (bool)\n"
        "- All required variables have values in every non-expect_failure scenario\n"
        "- Only variables from the ModuleSpec are used\n\n"
        + _build_planner_prompt(spec)
    )


async def plan_scenarios(spec: ModuleSpec, max_repair_iters: int = 2) -> ScenarioPlan:
    """Run the planner agent; repair up to max_repair_iters times on failure.

    Falls back to a deterministic minimal plan if all LLM attempts fail.
    """
    agent = _get_planner_agent()
    prompt = _build_planner_prompt(spec)

    for attempt in range(max_repair_iters + 1):
        try:
            result = await agent.run(prompt)
            plan: ScenarioPlan = result.data
            if not plan.scenarios:
                raise ValueError("LLM returned empty scenarios list")
            return plan
        except Exception as exc:
            print(f"[planner] Attempt {attempt + 1} failed: {exc}")
            prompt = _repair_prompt(spec, str(exc))

    print("[planner] All LLM attempts failed — using deterministic fallback plan")
    return _deterministic_fallback(spec)


def _deterministic_fallback(spec: ModuleSpec) -> ScenarioPlan:
    """Build a minimal scenario plan from the ModuleSpec without any LLM."""
    required_vals = {v.name: _infer_value(v.name, v.type, spec.provider) for v in spec.required_variables}
    optional_vals = {
        v.name: (v.default if v.default is not None else _infer_value(v.name, v.type, spec.provider))
        for v in spec.optional_variables
    }
    always_on = [r for r in spec.resources if not r.gated_by]
    always_ids = [f"{r.resource_type}.{r.resource_name}" for r in always_on]

    scenarios: list[ScenarioEntry] = [
        ScenarioEntry(
            name="minimal",
            description="Required variables only — baseline scenario.",
            var_values=required_vals,
            features_expected=always_ids,
        ),
        ScenarioEntry(
            name="maximal",
            description="All variables set including optional ones.",
            var_values={**required_vals, **optional_vals},
            features_expected=[f"{r.resource_type}.{r.resource_name}" for r in spec.resources],
        ),
    ]

    for gate in spec.feature_gates:
        gate_on_val = _gate_on_value(gate.gate_variable, gate.gate_type, spec)
        gate_off_val = _gate_off_value(gate.gate_variable, gate.gate_type)
        gate_on_resources = [r for r in spec.resources if r.gated_by == gate.gate_variable]
        gate_on_ids = [f"{r.resource_type}.{r.resource_name}" for r in gate_on_resources]

        scenarios.append(ScenarioEntry(
            name=f"gate_{gate.gate_variable}_on",
            description=f"Enable {gate.gate_variable} feature gate.",
            var_values={**required_vals, gate.gate_variable: gate_on_val},
            features_expected=always_ids + gate_on_ids,
        ))
        scenarios.append(ScenarioEntry(
            name=f"gate_{gate.gate_variable}_off",
            description=f"Disable {gate.gate_variable} feature gate.",
            var_values={**required_vals, gate.gate_variable: gate_off_val},
            features_expected=always_ids,
        ))

    return ScenarioPlan(
        module_name=spec.module_name,
        module_source=spec.module_source,
        scenarios=scenarios,
        metadata={"fallback": True, "provider": spec.provider},
    )


def _infer_value(var_name: str, var_type: str, provider: str) -> Any:
    """Infer a realistic value for a variable from its name and type."""
    name = var_name.lower()
    if "project" in name:
        return "my-test-project"
    if "region" in name or "location" in name:
        return {"google": "us-central1", "aws": "us-east-1", "azurerm": "East US"}.get(provider, "us-central1")
    if name in ("name", "module_name", "bucket_name"):
        return "test-module"
    if "environment" in name or name == "env":
        return "dev"
    if "account" in name:
        return "123456789012"
    if "group" in name:
        return "test-resource-group"
    if var_type == "bool":
        return False
    if var_type == "number":
        return 1
    if "list" in var_type or "set" in var_type:
        return []
    if "map" in var_type:
        return {}
    return "example-value"


def _gate_on_value(gate_var: str, gate_type: "GateType", spec: ModuleSpec) -> Any:
    """Return a value that enables the gate."""
    var_spec = next((v for v in spec.variables if v.name == gate_var), None)
    if var_spec:
        if var_spec.type == "bool":
            return True
        if "list" in var_spec.type or "set" in var_spec.type:
            return ["us-central1"]
        if "map" in var_spec.type:
            return {"key": "value"}
        if var_spec.enum_values:
            return var_spec.enum_values[0]
    return True


def _gate_off_value(gate_var: str, gate_type: "GateType") -> Any:
    """Return a value that disables the gate."""
    return False
