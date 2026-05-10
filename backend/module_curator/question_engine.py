"""
question_engine.py — Generate clarifying questions via the local LLM (Ollama).

The LLM is asked for a JSON array of strings.  If parsing fails, a set of
sensible fallback questions is returned so the pipeline never stalls.
"""
from __future__ import annotations

import json
import re

from openai import AsyncOpenAI

from backend.config import get_config
from backend.module_curator.models import CurationMode, CurationSession

MAX_QUESTIONS = 5


def _client() -> AsyncOpenAI:
    cfg = get_config()
    return AsyncOpenAI(
        base_url=cfg.llm.base_url.rstrip("/") + "/v1",
        api_key="ollama",
    )


async def generate_questions(session: CurationSession) -> list[str]:
    cfg = get_config()
    client = _client()

    ctx_parts = [
        "You are a Terraform infrastructure expert.",
        f"Cloud Provider: {session.provider.value}",
        f"Service / Product: {session.service_name or 'unspecified'}",
        f"Curation mode: {session.mode.value}",
    ]

    if session.document_text:
        ctx_parts.append(f"\nSpec document excerpt:\n{session.document_text[:2500]}")

    if session.tf_files:
        names = ", ".join(list(session.tf_files.keys())[:8])
        first_content = next(iter(session.tf_files.values()), "")[:1800]
        ctx_parts.append(f"\nExisting module files: {names}\nSample content:\n{first_content}")

    if session.registry_docs:
        ctx_parts.append(f"\nProvider documentation excerpt:\n{session.registry_docs[:2000]}")

    context = "\n".join(ctx_parts)

    prompt = (
        f"{context}\n\n"
        f"Generate exactly {MAX_QUESTIONS} clarifying questions to gather the information "
        "needed to write a complete, production-ready Terraform module.\n"
        "Focus on: deployment topology, security/IAM, naming conventions, "
        "cross-service integrations, and scaling requirements.\n"
        "Return ONLY a valid JSON array of question strings — no markdown, no extra text.\n"
        'Example output: ["Question 1?", "Question 2?"]'
    )

    try:
        resp = await client.chat.completions.create(
            model=cfg.llm.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=600,
        )
        raw = resp.choices[0].message.content.strip()
        raw = _strip_fences(raw)
        questions = json.loads(raw)
        if isinstance(questions, list):
            return [q for q in questions if isinstance(q, str)][:MAX_QUESTIONS]
    except Exception as exc:
        print(f"[question_engine] LLM question generation failed: {exc}")

    return _fallback_questions(session)


def _strip_fences(text: str) -> str:
    """Remove markdown code fences if the LLM wrapped the JSON."""
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = text.replace("```", "").strip()
    return text


def _fallback_questions(session: CurationSession) -> list[str]:
    service = session.service_name or "this service"
    mode = session.mode

    if mode == CurationMode.SELF_CURATION:
        return [
            f"What specific changes or new features should the new version of the {service} module include?",
            "Which existing resources should be modified, and which should remain unchanged?",
            "Are there any breaking changes that downstream consumers of the module must be aware of?",
            "What is the new tag version name and does it follow semantic versioning (e.g. v2.1.0)?",
            "Should the module maintain backward compatibility or is a clean break acceptable?",
        ]

    if mode == CurationMode.FROM_DOCUMENT:
        return [
            "What aspects of the specification are most critical and should be prioritised in the module?",
            "Are there environment-specific differences (dev / staging / prod) the module should handle?",
            "What naming and tagging conventions should the module enforce?",
            "Which IAM roles and service accounts does this service require?",
            "Are there any compliance or regulatory requirements (encryption at rest, VPC-only, CMEK)?",
        ]

    # NEW_PRODUCT or FROM_MODULE
    return [
        f"What is the primary workload or use case for the {service} module?",
        "Which cloud regions or availability zones should this module support?",
        "What are the security requirements — IAM roles, private networking, encryption keys?",
        "Should the module support multiple environments from a single call, or per-environment instances?",
        "Are there any specific naming conventions, resource labels, or cost-allocation tags required?",
    ]
