"""
run.py — CLI entry point for the scenario generator.

Usage:
  python -m backend.scenario_generator.run --source github --url https://github.com/org/module
  python -m backend.scenario_generator.run --source local --path ./my-module
  python -m backend.scenario_generator.run --source zip --file module.zip
  python -m backend.scenario_generator.run --source github --url https://... --tag v1.2.3

Options:
  --source   Source type: github | local | zip
  --url      GitHub URL (for --source github or local path shorthand)
  --path     Local directory path (for --source local)
  --file     ZIP file path (for --source zip)
  --tag      Git tag or branch (optional, for --source github)
  --no-test  Skip terraform test (validate only)
  --output   Override output directory

Pipeline:
  3a. load_module  → tf_files
  3b. parse_module → ModuleSpec   (deterministic, no LLM)
  3c. plan_scenarios → ScenarioPlan  (PydanticAI Agent)
  3d. synthesize   → GeneratedScenario[]
  3e. validate     → ValidationResult[]  (terraform validate + terraform test)
  3f. coverage     → CoverageReport + COVERAGE.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


async def _run(args: argparse.Namespace) -> int:
    from backend.scenario_generator.loader import load_module
    from backend.scenario_generator.parser import build_module_spec
    from backend.scenario_generator.planner import plan_scenarios
    from backend.scenario_generator.synthesizer import (
        build_output_dir,
        synthesize_scenario,
        write_scenario,
    )
    from backend.scenario_generator.validator import validate_all
    from backend.scenario_generator.coverage import build_coverage_report, write_coverage_md

    # ── 3a. Load ──────────────────────────────────────────────────────────────
    print(f"[run] Loading module — source_type={args.source}")
    try:
        tf_files = load_module(
            source_type=args.source,
            url=args.url or "",
            local_path=args.path or "",
            tag=args.tag or None,
        )
    except Exception as exc:
        print(f"[run] ERROR: Failed to load module: {exc}", file=sys.stderr)
        return 1

    if not tf_files:
        print("[run] ERROR: No .tf files found in the specified source.", file=sys.stderr)
        return 1

    print(f"[run] Loaded {len(tf_files)} .tf file(s): {', '.join(sorted(tf_files)[:8])}")

    # ── 3b. Parse ─────────────────────────────────────────────────────────────
    print("[run] Parsing module spec (deterministic, AST-based)…")
    module_name = _infer_module_name(args)
    spec = build_module_spec(tf_files, module_name=module_name, module_source=args.url or args.path or "")

    print(f"[run] ModuleSpec: provider={spec.provider}, "
          f"vars={len(spec.variables)} (req={len(spec.required_variables)}), "
          f"resources={len(spec.resources)}, gates={len(spec.feature_gates)}")

    # ── 3c. Plan ──────────────────────────────────────────────────────────────
    print("[run] Planning scenarios (PydanticAI Agent)…")
    plan = await plan_scenarios(spec)
    print(f"[run] ScenarioPlan: {len(plan.scenarios)} scenario(s)")
    for s in plan.scenarios:
        flag = " [expect_failure]" if s.expect_failure else ""
        print(f"       • {s.name}{flag}: {s.description[:60]}")

    # ── 3d. Synthesize ────────────────────────────────────────────────────────
    out_dir = Path(args.output) if args.output else build_output_dir(module_name)
    print(f"[run] Synthesizing scenario configs → {out_dir}")

    module_rel_path = _module_rel_path(args, out_dir)
    generated = []
    for entry in plan.scenarios:
        gs = synthesize_scenario(entry, spec, module_relative_path=module_rel_path)
        gs = write_scenario(gs, out_dir, module_rel_path)
        generated.append(gs)
        print(f"[run]   Written: {out_dir}/{entry.name}/")

    # ── 3e. Validate ──────────────────────────────────────────────────────────
    print("[run] Validating scenarios…")
    results = await validate_all(generated, spec, module_source_path=args.path or "")
    for vr in results:
        status = "SKIP" if vr.skipped else ("PASS" if vr.ok else "FAIL")
        fix_info = f" (fixed in {vr.iterations} iter)" if vr.fixed else ""
        print(f"[run]   {status}  {vr.scenario}{fix_info}")
        if not vr.ok and not vr.skipped and vr.stderr:
            print(f"         → {vr.stderr[:120]}")

    # ── 3f. Coverage ──────────────────────────────────────────────────────────
    report = build_coverage_report(spec, generated, results)
    write_coverage_md(report, out_dir)
    print(f"\n[run] Coverage: {report.passed_scenarios}/{report.total_scenarios} passed")
    if report.uncovered_gates:
        print(f"[run] ⚠ Uncovered gates: {', '.join(report.uncovered_gates)}")
    print(f"[run] COVERAGE.md written to {out_dir}/COVERAGE.md")

    # Write a machine-readable summary
    summary_path = out_dir / "summary.json"
    summary_path.write_text(
        json.dumps({
            "module_name": spec.module_name,
            "provider": spec.provider,
            "total_scenarios": report.total_scenarios,
            "passed_scenarios": report.passed_scenarios,
            "uncovered_gates": report.uncovered_gates,
            "output_dir": str(out_dir),
        }, indent=2),
        encoding="utf-8",
    )
    print(f"[run] summary.json written to {summary_path}")

    failures = sum(1 for vr in results if not vr.ok and not vr.skipped and not vr.scenario in
                   {gs.name for gs in generated if gs.based_on_scenario.expect_failure})
    return 0 if failures == 0 else 1


def _infer_module_name(args: argparse.Namespace) -> str:
    if args.url:
        return args.url.rstrip("/").split("/")[-1]
    if args.path:
        return Path(args.path).name
    return "module"


def _module_rel_path(args: argparse.Namespace, out_dir: Path) -> str:
    """Compute a relative path from a scenario subdir to the module source."""
    if args.path:
        # Local module: compute relative path from scenario dir to module dir
        try:
            module_abs = Path(args.path).resolve()
            scenario_abs = out_dir / "PLACEHOLDER"
            rel = Path(module_abs).relative_to(Path.cwd())
            return str(rel).replace("\\", "/")
        except ValueError:
            return str(Path(args.path).resolve()).replace("\\", "/")
    # GitHub / zip: assume we extracted to a temp dir; use a placeholder
    return "../../../../module"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TerraScope Scenario Generator — create and validate Terraform test configs"
    )
    parser.add_argument("--source", choices=["github", "local", "zip"], required=True,
                        help="Module source type")
    parser.add_argument("--url",  default="", help="GitHub URL (source=github)")
    parser.add_argument("--path", default="", help="Local directory path (source=local)")
    parser.add_argument("--file", default="", help="ZIP file path (source=zip)")
    parser.add_argument("--tag",  default="", help="Git tag or branch (source=github)")
    parser.add_argument("--no-test", action="store_true", help="Skip terraform test, validate only")
    parser.add_argument("--output", default="", help="Override output directory")

    args = parser.parse_args()

    # Validate required args per source type
    if args.source == "github" and not args.url:
        parser.error("--url is required when --source=github")
    if args.source == "local" and not args.path:
        parser.error("--path is required when --source=local")

    exit_code = asyncio.run(_run(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
