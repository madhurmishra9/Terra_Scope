"""
terrascope_agent.py — Two agents in one module.

QUERY AGENT  (existing)
  - temperature=0.0, strict grounding, output_type=AgentResponse
  - Answers questions about indexed repos with source citations
  - Anti-hallucination: refuses to answer without repo evidence

GENERATION AGENT  (new)
  - temperature=0.0, schema-grounded, output_type=str then parsed
  - 5-stage pipeline: Understand -> Plan -> Render -> Validate -> Self-Heal
  - Produces complete HCL files following TerraScope style conventions
  - Python-side post-validation via hcl_generator.validate_hcl_files()
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from backend.config import get_config
from backend.agent.models import (
    AgentResponse, QueryType, QueryRequest,
    SourceReference, IssueSolution,
    GenerateRequest, GenerationResponse, GenerationMode,
    GeneratedFile, ValidationNote,
)
from backend.agent.tools.git_tools import (
    list_tags_for_repo, get_latest_tag, get_file_at_tag,
    list_tf_files_at_tag, diff_tags, get_changelog,
)
from backend.agent.tools.hcl_tools import (
    get_all_variables, get_all_resources, get_outputs,
    get_provider_requirements, get_iam_bindings, summarize_module,
)
from backend.agent.tools.search_tools import semantic_search, is_indexed
from backend.agent.tools.issue_tools import match_known_issue, issues_for_product
from backend.agent.tools.hcl_generator import (
    build_generation_context,
    parse_generated_files,
    validate_hcl_files,
)


# ── Query agent ────────────────────────────────────────────────────────────────

def _build_query_system_prompt(strict: bool) -> str:
    cfg = get_config()
    mode_instruction = (
        """
STRICT GROUNDING MODE (ACTIVE):
- You MUST only answer using information found in the repository code retrieved by your tools.
- If the tools return no relevant code, respond: "I cannot find information about this in the indexed repository code."
- DO NOT use your general training knowledge about Terraform or GCP.
- EVERY statement in your answer must be traceable to a specific file and line number.
- If confidence is below 0.65, say explicitly: "I don't have enough indexed code to answer this reliably."
"""
        if strict
        else """
BALANCED MODE:
- Prefer repository code from tools over general knowledge.
- When repo code is insufficient, you may supplement with general Terraform/GCP knowledge,
  but CLEARLY label it as: "[General knowledge — not from repo code]"
- Still cite sources wherever possible.
"""
    )

    return f"""
You are TerraScope, a precise AI agent for the Terraform module curation team.
Your repository set covers Google Cloud data engineering products:
BigQuery, Cloud Storage, Dataflow, Pub/Sub, Dataproc, Cloud Composer, Spanner, Bigtable, and more.

{mode_instruction}

RESPONSE RULES:
1. Always call the appropriate tools first. Do NOT answer from memory.
2. For ANY question: call summarize_module() first to understand the codebase.
3. For issue questions: call match_known_issue() first. If it matches, use that solution.
4. Always provide line-level citations in your sources[].
5. For variable questions: call get_all_variables() — never guess variable names.
6. For resource questions: call get_all_resources() — never guess resource types.
7. For diffs: call diff_tags() — never describe changes without seeing the diff.
8. Assign confidence based ONLY on how much supporting code you found:
   - 0.9-1.0: Multiple files confirm the answer
   - 0.7-0.9: One file confirms the answer
   - 0.5-0.7: Partial evidence, answer with caveats
   - < 0.5: Return "I don't know"

NEVER fabricate:
- File names, line numbers, variable names, resource types, IAM roles
- Version numbers, provider constraints, output names
- Error causes or solutions not in the KB or repo code
"""


# ── Generation agent ───────────────────────────────────────────────────────────

_GENERATION_SYSTEM_PROMPT = """
You are TerraScope, an expert Terraform HCL engineer and infrastructure architect.
Your ONLY job is to write production-grade Terraform code for Google Cloud Platform.

GENERATION PIPELINE — follow strictly:

STAGE 1 — UNDERSTAND
  Parse the intent into a resource dependency graph (internal only, never output this).
  Identify all providers needed. Use the schema context provided.

STAGE 2 — PLAN (internal only, never output)
  Map resources to files. Identify variables, outputs, dependencies.
  Use version constraints from the schema context.

STAGE 3 — RENDER (this is the only output)
  Emit files using EXACTLY this structure:
  ---FILE: repos/{module}/versions.tf---
  {content}
  ---ENDFILE---
  ---FILE: repos/{module}/variables.tf---
  {content}
  ---ENDFILE---
  ---FILE: repos/{module}/main.tf---
  {content}
  ---ENDFILE---
  ---FILE: repos/{module}/outputs.tf---
  {content}
  ---ENDFILE---

STAGE 4 — VALIDATE (mentally, fix inline)
  - All required attributes present per schema.
  - No deprecated arguments used.
  - Variable types match usage.
  - Outputs reference valid resource attributes.
  - Flag issues as HCL comments: # [SECURITY]: ... / # [COST]: ... / # [TFLINT]: ...

STAGE 5 — SELF-HEAL
  Fix any issue found in Stage 4 before emitting output.

HCL GRAMMAR RULES (non-negotiable):
- 2-space indentation
- Double-quoted strings
- Dot notation for all references (resource.name.attribute)
- Blank line between every top-level block
- for_each preferred over count at all times
- dynamic blocks for optional nested configuration (keyed on != null check)
- No trailing commas in object literals
- No inline comments except [SECURITY]/[COST]/[TFLINT] flags

VARIABLE FORM — always use full form:
  variable "name" {
    type        = ...          # always explicit
    description = "..."        # always present
    default     = ...          # omit if required
    sensitive   = true/false   # explicit for secrets
    validation {
      condition     = ...
      error_message = "..."
    }
  }

OUTPUT FORM — always include description:
  output "name" {
    description = "..."
    value       = ...
    sensitive   = true/false
  }

COMMON LABELS — emit in every module:
  locals {
    common_labels = merge(
      { environment = var.environment, managed_by = "terraform", module = "MODULE_NAME" },
      var.additional_labels,
    )
  }
  Apply via: labels = merge(local.common_labels, var.labels) on all taggable resources.

SECURITY DEFAULTS (always apply unless explicitly overridden):
- google_storage_bucket: public_access_prevention = "enforced"
- google_dataproc_cluster: internal_ip_only = true
- google_dataflow_flex_template_job: ip_configuration = "WORKER_IP_PRIVATE"
- KMS encryption: use dynamic block keyed on var.kms_key_name != null
- IAM: use _iam_binding not _iam_policy; no wildcard roles

FORBIDDEN PATTERNS — never emit these:
- Hardcoded account IDs, regions, ARNs (use variables or data sources)
- count on resources that support for_each
- Sensitive values inline (use var.* with sensitive=true)
- Commented-out code blocks
- Any attribute not listed in the provided schema context
- depends_on unless no implicit dependency path exists
"""


def _build_generation_prompt(request: GenerateRequest, schema_context: str) -> str:
    """Build the full user-turn prompt for the generation agent."""
    mode_instruction = {
        GenerationMode.EXTEND: (
            f"TASK: Extend the existing module '{request.target_module}' with the following changes.\n"
            "OUTPUT: Use unified diff format for files that already exist (---FILE: path--- / diff / ---ENDFILE---).\n"
            "Only output files that actually change."
        ),
        GenerationMode.NEW: (
            f"TASK: Create a complete new Terraform module '{request.target_module}' from scratch.\n"
            "OUTPUT: All 4 files (versions.tf, variables.tf, main.tf, outputs.tf)."
        ),
        GenerationMode.COMPOSE: (
            f"TASK: Create a composite Terraform module '{request.target_module}' "
            "that wires together the listed existing modules.\n"
            "OUTPUT: All 4 files. Reference sibling modules with relative source paths."
        ),
    }[request.mode]

    base_repos_line = ""
    if request.base_repos:
        base_repos_line = f"\nBASE REPOS TO DRAW FROM: {', '.join(request.base_repos)}"

    return f"""
{mode_instruction}

INTENT:
{request.intent}

TARGET MODULE: repos/{request.target_module}/
GCP PRODUCT: {request.gcp_product or 'infer from intent'}
PROVIDER VERSION CONSTRAINT: {request.provider_version}{base_repos_line}

{schema_context}

Generate the files now. Output ONLY the ---FILE: ...---ENDFILE--- blocks.
"""


# ── Agent singletons ───────────────────────────────────────────────────────────

_query_agent:      Optional[Agent] = None
_generation_agent: Optional[Agent] = None


def _make_model() -> OpenAIChatModel:
    cfg = get_config()
    provider = OpenAIProvider(
        base_url=cfg.llm.base_url.rstrip("/") + "/v1",
        api_key="ollama",
    )
    return OpenAIChatModel(model_name=cfg.llm.model, provider=provider)


def get_agent() -> Agent:
    global _query_agent
    if _query_agent is None:
        _query_agent = Agent(
            model=_make_model(),
            output_type=AgentResponse,
            system_prompt=_build_query_system_prompt(strict=True),
        )
    return _query_agent


def get_generation_agent() -> Agent:
    """
    Separate agent for code generation.
    output_type=str so we can parse the file-marker format ourselves.
    """
    global _generation_agent
    if _generation_agent is None:
        _generation_agent = Agent(
            model=_make_model(),
            output_type=str,
            system_prompt=_GENERATION_SYSTEM_PROMPT,
        )
    return _generation_agent


# ── Query pipeline ─────────────────────────────────────────────────────────────

async def run_query(request: QueryRequest) -> AgentResponse:
    """
    Main query entry point.
    1. Resolve repo and tag
    2. Build rich context from code tools
    3. Run query agent
    4. Post-process: enforce confidence threshold, inject sources/solutions
    """
    cfg = get_config()
    agent = get_agent()

    # Resolve repo
    repo_name = request.repo_name
    if not repo_name:
        enabled = cfg.enabled_repos
        if not enabled:
            return AgentResponse(
                query_type=QueryType.UNKNOWN,
                answer="No repos are configured or enabled. Edit terrascope.config.yaml.",
                confidence=0.0,
                grounded=False,
                disclaimer="No repositories indexed.",
            )
        repo_name = enabled[0].name

    repo_cfg = cfg.get_repo(repo_name)
    if not repo_cfg:
        return AgentResponse(
            query_type=QueryType.UNKNOWN,
            answer=f"Repository '{repo_name}' not found in terrascope.config.yaml.",
            confidence=0.0,
            grounded=False,
        )

    # Resolve tag
    tag = request.tag or get_latest_tag(repo_name)
    if not tag:
        tag = "main"

    # Check index
    if not is_indexed(repo_name, tag):
        return AgentResponse(
            query_type=QueryType.UNKNOWN,
            answer=(
                f"Repository '{repo_name}' at tag '{tag}' has not been indexed yet. "
                "Please click 'Re-index' in the UI or run: python -m backend.indexer.repo_indexer"
            ),
            confidence=0.0,
            grounded=False,
            tags_analyzed=[tag],
            repo_name=repo_name,
            disclaimer="Run indexing before querying.",
        )

    # Build tool context
    summary    = summarize_module(repo_name, tag)
    top_sources = semantic_search(request.question, repo_name, tag, n_results=cfg.grounding.max_retrieval_chunks)
    issue_match = match_known_issue(request.question)

    context_block = _build_query_context(
        repo_cfg=repo_cfg,
        tag=tag,
        summary=summary,
        top_sources=top_sources,
        issue_match=issue_match,
    )
    full_prompt = f"{context_block}\n\nQUESTION: {request.question}"

    # Run agent
    try:
        result = await agent.run(full_prompt)
        response: AgentResponse = result.data
    except Exception as e:
        return AgentResponse(
            query_type=QueryType.UNKNOWN,
            answer=f"Agent error: {str(e)}. Check that Ollama is running with model '{cfg.llm.model}'.",
            confidence=0.0,
            grounded=False,
            repo_name=repo_name,
        )

    # Post-process
    response.repo_name     = repo_name
    response.tags_analyzed = [tag]

    if not response.sources and top_sources:
        response.sources = top_sources[:3]

    if issue_match and not response.issue_solution:
        response.issue_solution = issue_match

    threshold = cfg.grounding.min_confidence_threshold
    if response.confidence < threshold:
        response.disclaimer = (
            f"Low confidence ({response.confidence:.0%}). "
            f"The indexed code does not contain enough information to answer this reliably. "
            f"Check that the correct tag ('{tag}') is indexed and your question refers to this module."
        )

    response.grounded = len(response.sources) > 0
    return response


def _build_query_context(
    repo_cfg,
    tag: str,
    summary: dict,
    top_sources: list[SourceReference],
    issue_match: Optional[IssueSolution],
) -> str:
    parts = [
        "=== REPOSITORY CONTEXT ===",
        f"Repo: {repo_cfg.name} ({repo_cfg.display_name})",
        f"GCP Product: {repo_cfg.gcp_product}",
        f"Tag being analyzed: {tag}",
        "",
        "=== MODULE SUMMARY (from code analysis) ===",
        json.dumps(summary, indent=2),
        "",
    ]

    if top_sources:
        parts.append("=== MOST RELEVANT CODE CHUNKS (from semantic search) ===")
        for i, src in enumerate(top_sources, 1):
            parts.append(
                f"[Source {i}] {src.file_path} lines {src.line_start}-{src.line_end} "
                f"(relevance: {src.relevance:.2f}):\n{src.snippet}"
            )
        parts.append("")

    if issue_match:
        parts.append("=== KNOWN ISSUE MATCH (from KB — use this solution) ===")
        parts.append(f"Root cause: {issue_match.root_cause}")
        parts.append("Solution steps: " + " | ".join(issue_match.solution_steps))
        if issue_match.terraform_fix:
            parts.append(f"Terraform fix:\n{issue_match.terraform_fix}")
        parts.append("")

    return "\n".join(parts)


# ── Generation pipeline ────────────────────────────────────────────────────────

async def run_generation(request: GenerateRequest) -> GenerationResponse:
    """
    HCL generation pipeline.

    Stage 1-2: Build context (schemas + existing module patterns)
    Stage 3:   Call LLM with structured generation prompt
    Stage 4-5: Parse output, validate, self-heal via Python checks
    """
    agent = get_generation_agent()

    # Build context — schemas + existing code patterns
    context = build_generation_context(
        mode=request.mode.value,
        target_module=request.target_module,
        base_repos=request.base_repos,
        gcp_product=request.gcp_product,
    )

    # Build full prompt
    prompt = _build_generation_prompt(request, context)

    # Run LLM
    try:
        result = await agent.run(prompt)
        raw_output: str = result.data
    except Exception as e:
        return GenerationResponse(
            mode=request.mode,
            target_module=request.target_module,
            files=[],
            validation_notes=[ValidationNote(
                level="error",
                file="",
                message=f"LLM generation failed: {e}. Check that Ollama is running with a capable model.",
            )],
            ready=False,
            disclaimer=(
                f"Generation error: {e}. "
                "Try a more capable model (e.g. qwen2.5-coder:7b or llama3.1:8b) for code generation."
            ),
        )

    # Parse ---FILE:---ENDFILE--- markers
    files = parse_generated_files(raw_output, request.target_module)

    if not files:
        return GenerationResponse(
            mode=request.mode,
            target_module=request.target_module,
            files=[],
            validation_notes=[ValidationNote(
                level="error",
                file="",
                message=(
                    "No files could be parsed from LLM output. "
                    "The model may need a larger context window or a stronger model."
                ),
            )],
            ready=False,
            disclaimer=(
                "No file markers found in LLM output. "
                "Consider switching to qwen2.5-coder:7b or a model with >= 8k context."
            ),
        )

    # Validate (Stages 4-5)
    validation_notes = validate_hcl_files(files)

    # Count resources and variables from generated content
    resource_count = sum(
        len(re.findall(r'^resource\s+"', f.content, re.MULTILINE))
        for f in files if not f.is_diff
    )
    variable_count = sum(
        len(re.findall(r'^variable\s+"', f.content, re.MULTILINE))
        for f in files if not f.is_diff
    )

    error_notes    = [n for n in validation_notes if n.level == "error"]
    security_notes = [n for n in validation_notes if n.level == "security"]
    ready          = len(error_notes) == 0

    disclaimer = None
    if security_notes:
        disclaimer = (
            f"Review {len(security_notes)} security note(s) before applying. "
            "All generated code must be reviewed by a human before terraform apply."
        )
    elif not ready:
        disclaimer = f"{len(error_notes)} error(s) found — resolve before applying."

    return GenerationResponse(
        mode=request.mode,
        target_module=request.target_module,
        files=files,
        validation_notes=validation_notes,
        resource_count=resource_count,
        variable_count=variable_count,
        ready=ready,
        disclaimer=disclaimer,
    )
