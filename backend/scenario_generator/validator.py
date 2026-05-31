"""
validator.py — Terraform validation for generated scenarios.

Supports three validation depths:
  1. terraform validate only  (needs init; no creds required)
  2. terraform test with mock_provider blocks  (offline, no creds)
  3. Fixer loop: PydanticAI Agent that repairs a failing config (max_iters from config)

Provider mocking via .terraformrc:
  - Generates a .terraformrc pointing at a filesystem_mirror or network_mirror.
  - If no mirror is available, falls back to depth 1.
  - TF_CLI_CONFIG_FILE env var is set to point at the generated .terraformrc.

Config key: terrascope.config.yaml → scenario_generator:
  terraform_bin: terraform
  tf_cli_config_file: ./.terraformrc
  max_fix_iters: 3
  run_terraform_test: true
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from backend.config import get_config
from backend.scenario_generator.models import (
    GeneratedScenario,
    ModuleSpec,
    ValidationResult,
)

_fixer_agent: Optional[Agent] = None


# ── Config helpers ────────────────────────────────────────────────────────────

def _scenario_config() -> dict:
    """Read scenario_generator config block with safe defaults."""
    cfg = get_config()
    raw = getattr(cfg.terrascope, "scenario_generator", None) or {}
    if hasattr(raw, "__dict__"):
        raw = raw.__dict__
    elif not isinstance(raw, dict):
        raw = {}
    return {
        "terraform_bin": raw.get("terraform_bin", "terraform"),
        "tf_cli_config_file": raw.get("tf_cli_config_file", ""),
        "max_fix_iters": int(raw.get("max_fix_iters", 3)),
        "run_terraform_test": bool(raw.get("run_terraform_test", True)),
    }


def _find_terraform() -> Optional[str]:
    sc = _scenario_config()
    tf_bin = sc["terraform_bin"]
    if shutil.which(tf_bin):
        return tf_bin
    return None


# ── .terraformrc generation ───────────────────────────────────────────────────

def generate_terraformrc(mirror_path: Optional[str] = None) -> str:
    """Generate a .terraformrc that enables offline provider installation.

    If mirror_path points to a local filesystem mirror (providers pre-downloaded),
    this enables fully offline `terraform init`.  Otherwise generates a stub.

    Usage:
        tf_rc_path = Path("./.terraformrc")
        tf_rc_path.write_text(generate_terraformrc(mirror_path="./provider_mirror"))
        os.environ["TF_CLI_CONFIG_FILE"] = str(tf_rc_path.resolve())
    """
    if mirror_path and Path(mirror_path).exists():
        abs_mirror = str(Path(mirror_path).resolve()).replace("\\", "/")
        return (
            'provider_installation {\n'
            '  filesystem_mirror {\n'
            f'    path    = "{abs_mirror}"\n'
            '    include = ["registry.terraform.io/*/*"]\n'
            '  }\n'
            '  direct {\n'
            '    exclude = ["registry.terraform.io/*/*"]\n'
            '  }\n'
            '}\n'
        )
    # No mirror — allow direct download but note it requires network
    return (
        '# .terraformrc — no local mirror configured\n'
        '# For offline use, run: terraform providers mirror ./provider_mirror\n'
        '# then set mirror_path in scenario_generator.tf_cli_config_file\n'
        'provider_installation {\n'
        '  direct {}\n'
        '}\n'
    )


# ── tftest.hcl generation ─────────────────────────────────────────────────────

def generate_tftest_hcl(
    scenario: GeneratedScenario,
    spec: ModuleSpec,
    module_source: str = "../module",
) -> str:
    """Generate a Terraform test file using mock_provider blocks.

    mock_provider requires NO credentials and NO network access, so plan-time
    checks work completely offline.  The test asserts that expected resources
    appear in the plan.
    """
    provider = spec.provider or "google"
    entry = scenario.based_on_scenario
    expected_resources = entry.features_expected

    lines: list[str] = [
        f'# Terraform test — scenario: {scenario.name}',
        f'# {entry.description}',
        '',
        f'mock_provider "{provider}" {{}}',
        '',
        f'run "scenario_{_slug(scenario.name)}" {{',
        '  command = plan',
        '',
    ]

    # Override variables from tfvars
    if entry.var_values:
        lines.append('  variables {')
        for k, v in entry.var_values.items():
            lines.append(f'    {k} = {_hcl_literal(v)}')
        lines.append('  }')
        lines.append('')

    # Assert expected resources appear
    if expected_resources:
        lines.append('  assert {')
        conditions = ' && '.join(
            f'can({_resource_ref(rid)})'
            for rid in expected_resources[:5]
        )
        lines.append(f'    condition     = {conditions}')
        lines.append('    error_message = "Expected resources not found in plan"')
        lines.append('  }')

    lines += ['}', '']
    return '\n'.join(lines)


# ── Validation runner ─────────────────────────────────────────────────────────

async def run_validate(
    scenario: GeneratedScenario,
    module_source_path: str = "",
) -> ValidationResult:
    """Run terraform init + validate in a temp directory.

    Returns a ValidationResult.  If terraform is not available, marks as skipped.
    """
    sc = _scenario_config()
    tf_bin = _find_terraform()

    if not tf_bin:
        return ValidationResult(
            scenario=scenario.name,
            ok=True,
            skipped=True,
            skip_reason="terraform binary not found on PATH; install terraform to enable validation",
        )

    with tempfile.TemporaryDirectory(prefix="terrascope_scenario_") as tmpdir:
        tmp = Path(tmpdir)

        # Write scenario files
        for rel_path, content in scenario.files.items():
            dest = tmp / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

        # Write .terraformrc if configured
        env = os.environ.copy()
        rc_path_cfg = sc["tf_cli_config_file"]
        if rc_path_cfg and Path(rc_path_cfg).exists():
            env["TF_CLI_CONFIG_FILE"] = str(Path(rc_path_cfg).resolve())

        # terraform init -backend=false
        init_result = _run_tf(tf_bin, ["init", "-backend=false", "-no-color"], tmp, env)
        if init_result.returncode != 0:
            return ValidationResult(
                scenario=scenario.name,
                ok=False,
                command="terraform init -backend=false",
                stderr=init_result.stderr,
                stdout=init_result.stdout,
            )

        # terraform validate
        val_result = _run_tf(tf_bin, ["validate", "-no-color"], tmp, env)
        ok = val_result.returncode == 0
        return ValidationResult(
            scenario=scenario.name,
            ok=ok,
            command="terraform validate",
            stdout=val_result.stdout,
            stderr=val_result.stderr,
        )


async def run_terraform_test(
    scenario: GeneratedScenario,
    spec: ModuleSpec,
    module_source_path: str = "",
) -> ValidationResult:
    """Run terraform test with mock_provider in a temp directory.

    Requires terraform >= 1.6.  Falls back to run_validate if test fails to init.
    """
    sc = _scenario_config()
    tf_bin = _find_terraform()

    if not tf_bin:
        return ValidationResult(
            scenario=scenario.name,
            ok=True,
            skipped=True,
            skip_reason="terraform binary not found",
        )

    if not sc["run_terraform_test"]:
        return await run_validate(scenario, module_source_path)

    with tempfile.TemporaryDirectory(prefix="terrascope_test_") as tmpdir:
        tmp = Path(tmpdir)

        # Write scenario files + module symlink/copy
        for rel_path, content in scenario.files.items():
            dest = tmp / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

        # Write the tftest.hcl
        tftest = generate_tftest_hcl(scenario, spec)
        (tmp / f"{_slug(scenario.name)}.tftest.hcl").write_text(tftest, encoding="utf-8")

        env = os.environ.copy()
        env["TF_CLI_CONFIG_FILE"] = str(Path(sc["tf_cli_config_file"]).resolve()) \
            if sc["tf_cli_config_file"] and Path(sc["tf_cli_config_file"]).exists() \
            else env.get("TF_CLI_CONFIG_FILE", "")

        # terraform init
        init = _run_tf(tf_bin, ["init", "-backend=false", "-no-color"], tmp, env)
        if init.returncode != 0:
            # Fall back to validate
            return await run_validate(scenario, module_source_path)

        # terraform test
        test = _run_tf(tf_bin, ["test", "-no-color"], tmp, env)
        ok = test.returncode == 0
        return ValidationResult(
            scenario=scenario.name,
            ok=ok,
            command="terraform test",
            stdout=test.stdout,
            stderr=test.stderr,
        )


async def validate_all(
    scenarios: list[GeneratedScenario],
    spec: ModuleSpec,
    module_source_path: str = "",
) -> list[ValidationResult]:
    """Validate all scenarios; apply fixer loop on failures."""
    results: list[ValidationResult] = []
    sc = _scenario_config()

    for gs in scenarios:
        # Intentional expect_failure scenarios: just check it *does* fail
        if gs.based_on_scenario.expect_failure:
            vr = await run_validate(gs, module_source_path)
            vr = vr.model_copy(update={"ok": not vr.ok, "scenario": gs.name})
            results.append(vr)
            continue

        vr = await run_terraform_test(gs, spec, module_source_path)

        # Fixer loop
        if not vr.ok and not vr.skipped:
            fixed_gs, vr = await _fixer_loop(gs, spec, vr, sc["max_fix_iters"])

        results.append(vr)
    return results


# ── Fixer agent ───────────────────────────────────────────────────────────────

def _get_fixer_agent() -> Agent:
    global _fixer_agent
    if _fixer_agent is None:
        cfg = get_config()
        model = OpenAIChatModel(
            model_name=cfg.llm.model,
            provider=OpenAIProvider(
                base_url=cfg.llm.base_url.rstrip("/") + "/v1",
                api_key="ollama",
            ),
        )
        _fixer_agent = Agent(
            model=model,
            output_type=GeneratedScenario,
            system_prompt=(
                "You are a Terraform expert. Given a failing Terraform scenario (files dict) "
                "and the error output, return a corrected GeneratedScenario with the same name "
                "and fixed files. Only fix the specific errors; do not change other content. "
                "Output ONLY a valid JSON object matching the GeneratedScenario schema."
            ),
        )
    return _fixer_agent


async def _fixer_loop(
    gs: GeneratedScenario,
    spec: ModuleSpec,
    initial_vr: ValidationResult,
    max_iters: int,
) -> tuple[GeneratedScenario, ValidationResult]:
    """Attempt to repair a failing scenario up to max_iters times."""
    current_gs = gs
    current_vr = initial_vr
    agent = _get_fixer_agent()

    for i in range(1, max_iters + 1):
        prompt = (
            f"SCENARIO: {current_gs.name}\n"
            f"ERROR ({current_vr.command}):\n{current_vr.stderr or current_vr.stdout}\n\n"
            f"CURRENT FILES:\n{_files_to_text(current_gs.files)}\n\n"
            f"MODULE SPEC SUMMARY:\n"
            f"  provider: {spec.provider}\n"
            f"  required vars: {[v.name for v in spec.required_variables]}\n\n"
            "Fix the configuration and return the corrected GeneratedScenario JSON."
        )

        try:
            result = await agent.run(prompt)
            fixed_gs: GeneratedScenario = result.data
        except Exception as exc:
            print(f"[validator] Fixer iteration {i} failed: {exc}")
            break

        # Re-validate the fixed scenario
        new_vr = await run_terraform_test(fixed_gs, spec)
        if new_vr.ok:
            new_vr = new_vr.model_copy(update={"fixed": True, "iterations": i})
            return fixed_gs, new_vr

        current_gs = fixed_gs
        current_vr = new_vr
        print(f"[validator] Fixer iteration {i}: still failing")

    # Give up — return the last state with fixed=False, iterations=max_iters
    current_vr = current_vr.model_copy(update={"iterations": max_iters})
    return current_gs, current_vr


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run_tf(
    tf_bin: str,
    args: list[str],
    cwd: Path,
    env: dict,
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [tf_bin] + args,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _files_to_text(files: dict[str, str]) -> str:
    parts = []
    for path, content in files.items():
        parts.append(f"=== {path} ===\n{content}")
    return "\n\n".join(parts)


def _hcl_literal(val) -> str:
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        return f'"{val}"'
    if isinstance(val, list):
        return "[" + ", ".join(_hcl_literal(v) for v in val) + "]"
    if isinstance(val, dict):
        pairs = ", ".join(f'"{k}" = {_hcl_literal(v)}' for k, v in val.items())
        return "{" + pairs + "}"
    return f'"{val}"'


def _resource_ref(rid: str) -> str:
    """Convert 'google_storage_bucket.main' to a plan expression."""
    return rid


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_") or "scenario"
