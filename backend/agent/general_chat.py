"""
general_chat.py — Repo-aware general chat mode.

Unlike the query agent (which targets ONE repo+tag and refuses ungrounded
answers in strict mode), general chat:

  • Searches across ALL indexed repos automatically — no repo/tag selection
  • Answers general Terraform/GCP/AWS/Azure questions from LLM knowledge
    when the repos don't contain the answer (clearly labelled)
  • Maintains conversation history for follow-up questions
  • Attributes every repo-grounded statement to its source repo + file

Response contract: GeneralChatResponse with answer, mode (grounded /
general / mixed), and per-repo sources.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from backend.config import get_config
from backend.agent.models import SourceReference
from backend.agent.tools.search_tools import (
    search_across_repos,
    is_indexed,
)
from backend.agent.tools.git_tools import get_latest_tag


# ── Models ────────────────────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role:    str   # "user" | "assistant"
    content: str


class GeneralChatRequest(BaseModel):
    question: str
    history:  list[ChatMessage] = []     # prior turns, oldest first
    max_history_turns: int = 6           # cap context size for small local LLMs


class GeneralChatResponse(BaseModel):
    answer:       str
    mode:         str = Field(description="grounded | general | mixed")
    sources:      list[SourceReference] = []
    repos_searched: list[str] = []
    confidence:   float = Field(default=0.7, ge=0.0, le=1.0)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _collect_indexed_repos() -> dict[str, str]:
    """Return {repo_name: latest_indexed_tag} for every indexed repo."""
    cfg = get_config()
    result: dict[str, str] = {}
    for repo in cfg.enabled_repos:
        try:
            tag = get_latest_tag(repo.name)
        except Exception:
            tag = None
        if tag and is_indexed(repo.name, tag):
            result[repo.name] = tag
    return result


def _build_prompt(
    question: str,
    history: list[ChatMessage],
    max_turns: int,
    sources: list[SourceReference],
) -> str:
    parts: list[str] = []

    parts.append(
        "You are TerraScope's general assistant — an expert on Terraform, "
        "GCP, AWS, and Azure infrastructure. Answer the user's question "
        "helpfully and concisely.\n"
    )

    if sources:
        parts.append(
            "RELEVANT CODE FROM THE USER'S INDEXED REPOS (cite repo + file "
            "when you use these; they are the ground truth for questions "
            "about the user's own modules):\n"
        )
        for s in sources[:8]:
            parts.append(
                f"--- {s.repo_name} @ {s.tag} · {s.file_path} "
                f"(lines {s.line_start}-{s.line_end}, relevance {s.relevance}) ---\n"
                f"{s.snippet}\n"
            )
        parts.append(
            "\nIf the question is about the user's modules, answer ONLY from "
            "the code above and name the repo/file. If the code above is not "
            "relevant to the question, ignore it and answer from your general "
            "knowledge — but say 'From general knowledge:' when you do.\n"
        )
    else:
        parts.append(
            "No relevant code was found in the user's indexed repos for this "
            "question. Answer from your general Terraform/cloud knowledge and "
            "begin with 'From general knowledge:'. If the question clearly "
            "concerns the user's own modules, say the repos may not be "
            "indexed yet and suggest indexing from the sidebar.\n"
        )

    if history:
        parts.append("\nCONVERSATION SO FAR:")
        for msg in history[-max_turns * 2:]:
            who = "User" if msg.role == "user" else "Assistant"
            parts.append(f"{who}: {msg.content}")

    parts.append(f"\nUser: {question}\nAssistant:")
    return "\n".join(parts)


# ── Entry point ───────────────────────────────────────────────────────────────

async def run_general_chat(req: GeneralChatRequest) -> GeneralChatResponse:
    cfg = get_config()

    # 1. Search every indexed repo (best-effort)
    repo_tags = _collect_indexed_repos()
    sources: list[SourceReference] = []
    if repo_tags:
        try:
            sources = search_across_repos(req.question, repo_tags, n_per_repo=3)
            # Keep only reasonably relevant hits
            sources = [s for s in sources if s.relevance >= 0.25][:8]
        except Exception as e:
            print(f"[general_chat] cross-repo search failed (non-fatal): {e}")

    # 2. Ask the LLM (plain completion — flexible, conversational)
    prompt = _build_prompt(req.question, req.history, req.max_history_turns, sources)

    try:
        import httpx
        from openai import AsyncOpenAI
        base_url = cfg.llm.base_url.rstrip("/") + "/v1"
        client = AsyncOpenAI(
            base_url=base_url,
            api_key="ollama",
            http_client=httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(120.0)),
        )
        resp = await client.chat.completions.create(
            model=cfg.llm.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,                      # slightly creative for chat
            max_tokens=cfg.llm.max_tokens,
        )
        answer = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        raise RuntimeError(f"LLM call failed: {e}") from e

    # 3. Classify the mode
    used_general = "from general knowledge" in answer.lower()
    if sources and not used_general:
        mode, confidence = "grounded", 0.85
    elif sources and used_general:
        mode, confidence = "mixed", 0.7
    else:
        mode, confidence = "general", 0.6

    return GeneralChatResponse(
        answer=answer,
        mode=mode,
        sources=sources if mode != "general" else [],
        repos_searched=list(repo_tags.keys()),
        confidence=confidence,
    )
