# TerraScope Build Summary — Typed Sequential Pipeline + Scenario Generator

*Completed: 2026-05-29*

---

## New Files Created

### backend/pipeline/
| File | Purpose |
|------|---------|
| `__init__.py` | Package export of QuestionSet, GenerationPassResult, CurationPipelineOutput, CurationPipeline |
| `models.py` | Typed Pydantic v2 output models for each curation stage |
| `runner.py` | CurationPipeline sequential runner (calls _run_pass_a/b/c in order) |

### backend/scenario_generator/
| File | Purpose |
|------|---------|
| `__init__.py` | Package docstring |
| `models.py` | All Pydantic v2 models: ModuleSpec, ScenarioPlan, GeneratedScenario, ValidationResult, CoverageReport, ScenarioSession |
| `loader.py` | Thin wrapper over module_fetcher (GitHub/local/ZIP/tf dispatch) |
| `parser.py` | Deterministic AST-based ModuleSpec builder — NO LLM; extends hcl_tools |
| `planner.py` | PydanticAI Agent (output_type=ScenarioPlan) + repair loop + deterministic fallback |
| `synthesizer.py` | Config assembly from ModuleSpec + ScenarioEntry — only LLM for realistic values |
| `validator.py` | terraform validate + terraform test (mock_provider) + PydanticAI fixer loop |
| `coverage.py` | CoverageReport builder + COVERAGE.md writer |
| `run.py` | CLI entry point (python -m backend.scenario_generator.run) |

### tests/
| File | Purpose |
|------|---------|
| `test_pipeline.py` | 15 unit tests for pipeline models (QuestionSet, GenerationPassResult, CurationPipelineOutput) |
| `test_scenario_models.py` | 20+ round-trip tests for all scenario generator Pydantic models |
| `test_scenario_generator.py` | 30+ integration tests: parser, synthesizer, planner fallback, coverage, validator utilities |
| `fixtures/sample_module/` | GCS bucket module with feature gates for test coverage |

### audit/
| File | Purpose |
|------|---------|
| `plan.md` | Phase 0 recon findings and per-phase file plan |
| `build-summary.md` | This file |

---

## Modified Files

| File | Changes |
|------|---------|
| `backend/module_curator/question_engine.py` | Replaced raw `AsyncOpenAI` JSON parsing with two PydanticAI Agents (output_type=QuestionSet) for questions and follow-ups. Fallback chain preserved. |
| `backend/module_curator/code_generator.py` | Added `_run_pass_a`, `_run_pass_b`, `_run_pass_c` typed wrappers returning `GenerationPassResult`. Existing 3-pass logic unchanged. |
| `backend/main.py` | Added `/api/scenarios/*` routes (start, upload-module, set-source, get, generate, rerun) with `_scenario_sessions` state dict and BackgroundTasks pipeline. |
| `frontend/src/App.jsx` | Added 4th tab "🧪 Scenarios" with ScenariosPanel component: source input, state display, scenario list with PASS/FAIL badges, tabbed code viewer, coverage table, re-run button. |
| `terrascope.config.yaml` | Added `scenario_generator:` block with terraform_bin, tf_cli_config_file, max_fix_iters, run_terraform_test. |
| `requirements.txt` | Added documentation of terraform binary requirement (external, non-pip). |

---

## How the Typed Sequential Pipeline Works

### Phase 1: question_engine (question stage)

Before (v2.1):
```python
raw = await client.chat.completions.create(...)
parsed = json.loads(_strip_fences(raw))   # manual, fails on non-JSON output
```

After (v2.2):
```python
agent = Agent(model=_make_model(), output_type=QuestionSet, system_prompt=...)
result = await agent.run(prompt)
qs: QuestionSet = result.data            # validated Pydantic model, never raw JSON
```

PydanticAI handles JSON extraction, schema validation, and retry internally. The
provider-specific fallback questions are still invoked on any exception, so the
pipeline never stalls.

### Phase 1: code_generator (passes A/B/C)

The three LLM passes are now also callable as individual typed functions:
```python
pass_a: GenerationPassResult = await _run_pass_a(session)
pass_b: GenerationPassResult = await _run_pass_b(session, pass_a.raw)
pass_c: GenerationPassResult = await _run_pass_c(session, all_files)
```

Each returns a `GenerationPassResult(pass_name, raw, files, fallback_used)`.
The original `generate_terraform_code()` is unchanged and still the default path.
CurationPipeline.run_generation() calls the typed wrappers and aggregates.

---

## How Accuracy is Enforced (scenario generator)

### Layer 1: Deterministic AST parsing
`parser.py` uses `python-hcl2` to extract variables, outputs, resources, data
sources, and module calls. Feature gates are detected via regex patterns on raw
HCL text (count/for_each/dynamic keyed on `var.X`). No LLM involvement.

### Layer 2: Typed output_type for LLM calls
All PydanticAI Agents use `output_type=<PydanticModel>`. PydanticAI validates
the LLM output against the schema and raises on failure, triggering the repair loop.

### Layer 3: Repair loop
`planner.py` retries the planner Agent up to `max_repair_iters` times (default 2)
with an error-specific repair prompt. If all retries fail, `_deterministic_fallback`
builds a guaranteed-valid ScenarioPlan from the ModuleSpec without any LLM.

### Layer 4: Deterministic synthesis
`synthesizer.py` assembles root configs from typed ModuleSpec + ScenarioEntry data.
Only `_hcl_value()` converts Python values to HCL literals — no LLM generates
structural code. No variables absent from ModuleSpec are ever injected.

### Layer 5: terraform validate + terraform test
`validator.py` runs `terraform init -backend=false` + `terraform validate` to catch
structural errors, then `terraform test` with `mock_provider` blocks for plan-level
coverage without cloud credentials.

### Layer 6: Fixer Agent
If validation fails, a PydanticAI Agent (output_type=GeneratedScenario) attempts
to repair the failing config. The repaired scenario is re-validated immediately.
Loops up to `max_fix_iters` (config key).

---

## Known qwen2.5-coder:7b Limitations

- **Non-JSON output**: qwen2.5-coder:7b often prefixes JSON with prose or markdown fences.
  PydanticAI's output_type extraction handles this better than raw `json.loads`,
  but very long outputs may still be truncated mid-object. The repair loop addresses
  truncation by re-asking with a simpler prompt.

- **ScenarioPlan complexity**: The planner prompt is deliberately detailed to force
  correct structure, but qwen2.5-coder:7b may produce fewer scenarios than requested.
  The `_deterministic_fallback` always produces a valid minimal+maximal+gate matrix.

- **Temperature**: temperature=0.0 is used for all factual stages. Only the
  question stage uses 0.3 (small temperature to vary question phrasing).

---

## Provider Mocking and Offline Validation

To validate scenarios without cloud credentials:

1. **mock_provider** (terraform test, tf >= 1.6): Generates `<scenario>.tftest.hcl`
   with `mock_provider "<provider>" {}` blocks. The plan runs without any real API
   calls or credentials. `run_terraform_test()` in validator.py uses this path.

2. **Offline provider install** (.terraformrc): For `terraform init` without network:
   ```bash
   # Mirror providers (run once, with network):
   terraform providers mirror ./provider_mirror
   # Point .terraformrc at the mirror:
   # scenario_generator.tf_cli_config_file: ./.terraformrc
   ```
   `generate_terraformrc(mirror_path="./provider_mirror")` generates the correct
   .terraformrc with a `filesystem_mirror` block.

3. **Graceful degradation**: If terraform is not installed, all validation results
   are marked `skipped=True` with a clear `skip_reason`. The pipeline completes
   and produces configs + coverage report regardless.

---

## Scenario Matrix Coverage

For each module, the generator produces:

| Scenario type | Conditions |
|---------------|-----------|
| `minimal` | Required variables only |
| `maximal` | All variables set |
| `gate_<var>_on` | One per feature gate — variable set to enable |
| `gate_<var>_off` | One per feature gate — variable set to disable |
| `boundary` | For vars with validation/enum: valid edge + one invalid (expect_failure=true) |
| `foreach_zero` | For for_each gates: variable set to [] (count = 0) |
| `foreach_many` | For for_each gates: variable set to list with 2-3 entries |

COVERAGE.md in the output directory maps each scenario → features/vars exercised
and flags any feature_gate not covered.
