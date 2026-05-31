"""
backend/scenario_generator — Typed sequential pipeline for Terraform module scenario generation.

Stages (in order):
  1. load_module   — load .tf files via module_fetcher
  2. parse_module  — deterministic AST-based ModuleSpec + feature-gate detection
  3. plan_scenarios — PydanticAI Agent → ScenarioPlan (matrix of scenarios)
  4. synthesize    — assemble GeneratedScenario configs deterministically
  5. validate      — terraform init/validate + tftest.hcl with mock_provider
  6. coverage      — produce CoverageReport and COVERAGE.md

Run via CLI: python -m backend.scenario_generator.run --source <url|path>
"""
