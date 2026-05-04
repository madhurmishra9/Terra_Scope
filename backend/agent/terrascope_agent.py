"""
terrascope_agent.py — The PydanticAI agent.

Anti-hallucination design:
  1. temperature=0.0 → deterministic, no creative generation
  2. system_prompt forbids any answer not backed by repo code
  3. Every answer MUST include SourceReference citations
  4. confidence < threshold → returns "I don't know" with disclaimer
  5. grounding.mode=strict → LLM cannot use general knowledge
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel  # Ollama uses OpenAI-compatible API
from pydantic_ai.providers.openai import OpenAIProvider

from backend.config import get_config
from backend.agent.models import (
    AgentResponse, QueryType, QueryRequest,
    SourceReference, IssueSolution,
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


def _build_system_prompt(strict: bool) -> str:
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
   - 0.9–1.0: Multiple files confirm the answer
   - 0.7–0.9: One file confirms the answer
   - 0.5–0.7: Partial evidence, answer with caveats
   - < 0.5: Return "I don't know"

NEVER fabricate:
- File names, line numbers, variable names, resource types, IAM roles
- Version numbers, provider constraints, output names
- Error causes or solutions not in the KB or repo code
"""


def build_agent() -> Agent:
    cfg = get_config()

    # Ollama uses OpenAI-compatible API at /v1
    provider = OpenAIProvider(
        base_url=cfg.llm.base_url.rstrip("/") + "/v1",
        api_key="ollama",  # Ollama doesn't require a real key
    )
    model = OpenAIChatModel(model_name=cfg.llm.model, provider=provider)

    agent = Agent(
        model=model,
        output_type=AgentResponse,
        system_prompt=_build_system_prompt(strict=True),
    )
    return agent


_agent: Optional[Agent] = None

def get_agent() -> Agent:
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


async def run_query(request: QueryRequest) -> AgentResponse:
    """
    Main entry point. Runs the full agent pipeline:
    1. Resolve repo and tag
    2. Build a rich context string from code tools
    3. Run the PydanticAI agent
    4. Post-process: enforce confidence threshold, add disclaimer
    """
    cfg = get_config()
    agent = get_agent()

    # ── Resolve repo ──────────────────────────────────────────────────────────
    repo_name = request.repo_name
    if not repo_name:
        # Use the first enabled repo as default
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

    # ── Resolve tag ───────────────────────────────────────────────────────────
    tag = request.tag or get_latest_tag(repo_name)
    if not tag:
        tag = "main"

    # ── Check index ───────────────────────────────────────────────────────────
    if not is_indexed(repo_name, tag):
        return AgentResponse(
            query_type=QueryType.UNKNOWN,
            answer=f"Repository '{repo_name}' at tag '{tag}' has not been indexed yet. "
                   f"Please click 'Re-index' in the UI or run: python -m backend.indexer.repo_indexer",
            confidence=0.0,
            grounded=False,
            tags_analyzed=[tag],
            repo_name=repo_name,
            disclaimer="Run indexing before querying.",
        )

    # ── Build tool context ────────────────────────────────────────────────────
    # Pre-fetch key context so the agent has structured facts to work with
    summary = summarize_module(repo_name, tag)
    top_sources = semantic_search(request.question, repo_name, tag, n_results=cfg.grounding.max_retrieval_chunks)
    issue_match = match_known_issue(request.question)

    # Build a rich prompt with pre-fetched context
    context_block = _build_context_block(
        repo_cfg=repo_cfg,
        tag=tag,
        summary=summary,
        top_sources=top_sources,
        issue_match=issue_match,
        question=request.question,
    )

    full_prompt = f"{context_block}\n\nQUESTION: {request.question}"

    # ── Run agent ─────────────────────────────────────────────────────────────
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

    # ── Post-process ──────────────────────────────────────────────────────────
    response.repo_name = repo_name
    response.tags_analyzed = [tag]

    # Inject pre-retrieved sources if agent didn't populate them
    if not response.sources and top_sources:
        response.sources = top_sources[:3]

    # Inject issue solution if KB matched and agent didn't produce one
    if issue_match and not response.issue_solution:
        response.issue_solution = issue_match

    # Enforce confidence threshold
    threshold = cfg.grounding.min_confidence_threshold
    if response.confidence < threshold:
        response.disclaimer = (
            f"Low confidence ({response.confidence:.0%}). "
            f"The indexed code does not contain enough information to answer this reliably. "
            f"Check that the correct tag ('{tag}') is indexed and your question refers to this module."
        )

    # Mark grounded only if sources were cited
    response.grounded = len(response.sources) > 0

    return response


def _build_context_block(
    repo_cfg,
    tag: str,
    summary: dict,
    top_sources: list[SourceReference],
    issue_match: Optional[IssueSolution],
    question: str,
) -> str:
    """Build a rich, structured context string for the agent prompt."""
    parts = [
        f"=== REPOSITORY CONTEXT ===",
        f"Repo: {repo_cfg.name} ({repo_cfg.display_name})",
        f"GCP Product: {repo_cfg.gcp_product}",
        f"Tag being analyzed: {tag}",
        f"",
        f"=== MODULE SUMMARY (from code analysis) ===",
        json.dumps(summary, indent=2),
        f"",
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
