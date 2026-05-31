"""
coverage.py — Build CoverageReport and write COVERAGE.md to the output dir.
"""
from __future__ import annotations

from pathlib import Path

from backend.scenario_generator.models import (
    CoverageReport,
    GeneratedScenario,
    ModuleSpec,
    ScenarioCoverage,
    ValidationResult,
)


def build_coverage_report(
    spec: ModuleSpec,
    scenarios: list[GeneratedScenario],
    validation_results: list[ValidationResult],
) -> CoverageReport:
    """Build a CoverageReport from generated scenarios and their validation results."""
    val_by_name = {v.scenario: v for v in validation_results}
    all_gates = {g.gate_variable for g in spec.feature_gates}
    all_vars  = {v.name for v in spec.variables}

    covered_gates: set[str] = set()
    covered_vars:  set[str] = set()
    per_scenario:  list[ScenarioCoverage] = []

    passed = 0
    for gs in scenarios:
        scenario = gs.based_on_scenario
        vr = val_by_name.get(gs.name)
        is_passed = (vr.ok if vr else True)
        if is_passed and not scenario.expect_failure:
            passed += 1

        # Which gates does this scenario toggle?
        exercised_gates = [
            g.gate_variable for g in spec.feature_gates
            if g.gate_variable in scenario.var_values
        ]
        covered_gates.update(exercised_gates)

        set_vars = list(scenario.var_values.keys())
        covered_vars.update(set_vars)

        per_scenario.append(ScenarioCoverage(
            scenario_name=gs.name,
            variables_set=set_vars,
            features_exercised=exercised_gates,
            resources_expected=scenario.features_expected,
            passed=is_passed,
        ))

    uncovered_gates = sorted(all_gates - covered_gates)
    uncovered_vars  = sorted(all_vars - covered_vars)

    return CoverageReport(
        module_name=spec.module_name,
        total_scenarios=len(scenarios),
        passed_scenarios=passed,
        per_scenario=per_scenario,
        uncovered_gates=uncovered_gates,
        uncovered_variables=uncovered_vars,
    )


def write_coverage_md(report: CoverageReport, output_dir: Path) -> None:
    """Write COVERAGE.md to the output directory."""
    lines: list[str] = [
        f"# Scenario Coverage Report — {report.module_name}",
        "",
        f"**Total scenarios**: {report.total_scenarios}  ",
        f"**Passed**: {report.passed_scenarios}  ",
        f"**Failed**: {report.total_scenarios - report.passed_scenarios}",
        "",
        "## Per-Scenario Summary",
        "",
        "| Scenario | Pass | Variables Set | Gates Exercised | Resources Expected |",
        "|----------|------|--------------|-----------------|-------------------|",
    ]
    for sc in report.per_scenario:
        status = "✅" if sc.passed else "❌"
        vars_s  = ", ".join(sc.variables_set[:5]) or "(none)"
        gates_s = ", ".join(sc.features_exercised) or "(none)"
        res_s   = ", ".join(sc.resources_expected[:4]) or "(any)"
        lines.append(f"| `{sc.scenario_name}` | {status} | {vars_s} | {gates_s} | {res_s} |")

    lines += ["", "## Feature Gate Coverage", ""]
    if report.uncovered_gates:
        lines.append("### ⚠️ Uncovered Gates")
        for g in report.uncovered_gates:
            lines.append(f"- `{g}` — no scenario toggles this gate")
    else:
        lines.append("✅ All feature gates are exercised by at least one scenario.")

    lines += ["", "## Variable Coverage", ""]
    if report.uncovered_variables:
        lines.append("### ℹ️ Variables Not Explicitly Set")
        for v in report.uncovered_variables:
            lines.append(f"- `{v}`")
    else:
        lines.append("✅ All variables are set in at least one scenario.")

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "COVERAGE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
