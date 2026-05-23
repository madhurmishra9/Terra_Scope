"""
question_engine.py — Generate clarifying questions via the local LLM (Ollama).

v2.1 changes
============
* Context-aware question generation. Inspects the resolved local modules
  (./repos/) and dependent modules and asks the user to fill in their
  REQUIRED inputs explicitly, instead of relying purely on a generic
  7-topic checklist.
* Two-phase Q&A. After the first round of answers, `generate_followups()`
  examines the answers for gaps and ambiguities and returns up to
  MAX_FOLLOWUPS extra questions. If nothing is missing, returns []
  and the session moves to READY.

If the LLM call fails for any reason, both phases degrade gracefully to
deterministic fallbacks so the pipeline never stalls.
"""
from __future__ import annotations

import json
import re

from openai import AsyncOpenAI

from backend.config import get_config
from backend.module_curator.models import CurationMode, CurationSession

MAX_QUESTIONS  = 7
MAX_FOLLOWUPS  = 3


def _client() -> AsyncOpenAI:
    cfg = get_config()
    return AsyncOpenAI(
        base_url=cfg.llm.base_url.rstrip("/") + "/v1",
        api_key="ollama",
    )


# ── Context assembly ─────────────────────────────────────────────────────────

def _build_local_modules_section(session: CurationSession) -> str:
    """Summarise matching modules from ./repos/ for the prompt."""
    if not session.local_modules:
        return ""
    lines = ["LOCAL MODULES FROM ./repos/ THAT MATCH THIS REQUEST:"]
    for m in session.local_modules[:5]:
        req = ", ".join(m.required_inputs[:6]) or "(none)"
        res = ", ".join(m.resource_types[:6]) or "(none)"
        lines.append(f"  • {m.name} ({m.rel_path})")
        lines.append(f"      resource types : {res}")
        lines.append(f"      required inputs: {req}")
    lines.append(
        "If any of these match the user's intent, the module to be generated "
        "should CALL them rather than re-implement their resources."
    )
    return "\n".join(lines)


def _build_dependent_modules_section(session: CurationSession) -> str:
    if not session.dependent_modules:
        return ""
    lines = ["RESOLVED DEPENDENT MODULES (module {} sources in the loaded code):"]
    for d in session.dependent_modules[:6]:
        req = ", ".join(d.required_inputs[:6]) or "(none)"
        outs = ", ".join(d.outputs[:5]) or "(none)"
        lines.append(f"  • {d.raw_source}  [{d.kind}, depth {d.depth}]")
        lines.append(f"      required inputs: {req}")
        lines.append(f"      outputs        : {outs}")
    return "\n".join(lines)


def _build_context(session: CurationSession) -> str:
    parts: list[str] = [
        "You are a senior Terraform infrastructure engineer.",
        f"Cloud Provider : {session.provider.value}",
        f"Service/Product: {session.service_name or 'unspecified'}",
        f"Curation mode  : {session.mode.value}",
    ]

    if session.document_text:
        parts.append(f"\nSpec document excerpt:\n{session.document_text[:2500]}")

    if session.tf_files:
        names = ", ".join(list(session.tf_files.keys())[:8])
        first = next(iter(session.tf_files.values()), "")[:1500]
        parts.append(f"\nExisting module files: {names}\nSample content:\n{first}")

    local = _build_local_modules_section(session)
    if local:
        parts.append("\n" + local)

    deps = _build_dependent_modules_section(session)
    if deps:
        parts.append("\n" + deps)

    if session.registry_docs and not session.registry_docs.startswith("["):
        parts.append(f"\nProvider documentation excerpt:\n{session.registry_docs[:1800]}")

    return "\n".join(parts)


# ── Targeted variable questions ──────────────────────────────────────────────

def _required_vars_to_ask(session: CurationSession) -> list[str]:
    """
    Extract required variables from the local + dependent modules that the
    USER must supply values for. We ask one direct question per variable so
    the generated code can fill them in correctly.
    """
    asked: list[str] = []
    seen: set[str] = set()

    def _add(var_name: str, source_label: str) -> None:
        if var_name in seen:
            return
        seen.add(var_name)
        asked.append(
            f"What value should be used for `{var_name}` "
            f"(required input of {source_label})?"
        )

    for m in session.local_modules[:3]:
        for var in m.required_inputs[:4]:
            _add(var, f"local module {m.name}")
        if len(asked) >= MAX_QUESTIONS - 2:
            break

    for d in session.dependent_modules[:3]:
        for var in d.required_inputs[:3]:
            _add(var, f"dependent module {d.raw_source}")
        if len(asked) >= MAX_QUESTIONS:
            break

    return asked[: MAX_QUESTIONS - 2]   # leave room for at least 2 generic questions


# ── Primary question generation ──────────────────────────────────────────────

async def generate_questions(session: CurationSession) -> list[str]:
    """
    Produce up to MAX_QUESTIONS clarifying questions.

    Strategy:
      1. Pre-seed with targeted questions for required inputs of local +
         dependent modules (deterministic, never wrong).
      2. Ask the LLM for the REMAINING slots, covering uncovered topics.
      3. If the LLM fails or returns garbage, top up with provider-specific
         fallback questions.
    """
    targeted = _required_vars_to_ask(session)
    remaining_slots = max(0, MAX_QUESTIONS - len(targeted))

    llm_questions: list[str] = []
    if remaining_slots > 0:
        llm_questions = await _ask_llm_for_questions(session, remaining_slots, targeted)

    questions = targeted + llm_questions

    if len(questions) < MAX_QUESTIONS:
        # Top up with fallback
        fallback = _fallback_questions(session)
        for q in fallback:
            if len(questions) >= MAX_QUESTIONS:
                break
            if q not in questions:
                questions.append(q)

    return questions[:MAX_QUESTIONS]


async def _ask_llm_for_questions(
    session: CurationSession,
    n: int,
    already_asked: list[str],
) -> list[str]:
    cfg = get_config()
    client = _client()
    context = _build_context(session)

    already = "\n".join(f"  - {q}" for q in already_asked) or "  (none yet)"

    prompt = (
        f"{context}\n\n"
        "I have already prepared these clarifying questions for the user:\n"
        f"{already}\n\n"
        f"Generate {n} ADDITIONAL questions covering whichever of these areas "
        "are not yet addressed (one question each — pick the most important):\n"
        "  - Primary workload, region(s), expected scale\n"
        "  - Network topology: VPC/VNet placement, private vs public endpoints\n"
        "  - IAM / identity: service accounts, roles, least-privilege\n"
        "  - Encryption: CMEK/KMS/CMK, TLS, key rotation\n"
        "  - High-availability & scaling: multi-region, replicas\n"
        "  - Naming and tagging conventions\n"
        "  - Cross-service integrations\n"
        "Return ONLY a valid JSON array of question strings. No markdown, "
        "no commentary. Example: [\"Question A?\", \"Question B?\"]"
    )

    try:
        resp = await client.chat.completions.create(
            model=cfg.llm.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=500,
        )
        raw = _strip_fences(resp.choices[0].message.content.strip())
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            cleaned = [q.strip() for q in parsed if isinstance(q, str) and q.strip()]
            return cleaned[:n]
    except Exception as exc:
        print(f"[question_engine] LLM question generation failed: {exc}")
    return []


# ── Follow-up question generation ────────────────────────────────────────────

async def generate_followups(session: CurationSession) -> list[str]:
    """
    Inspect Q&A so far and return follow-up questions for gaps / ambiguities.

    Returns [] when the answers cover everything the generator needs.
    """
    if session.followup_round >= session.max_followup_rounds:
        return []

    cfg = get_config()
    client = _client()
    context = _build_context(session)

    qa_block = "\n".join(
        f"Q: {qa.question}\nA: {qa.answer}" for qa in session.qa_pairs
    )

    prompt = (
        f"{context}\n\n"
        "Below are the user's answers to the initial clarifying questions:\n\n"
        f"{qa_block}\n\n"
        "Review these answers against the requirements needed to write a 100%-accurate "
        "Terraform module that will pass `terraform validate`. Identify any:\n"
        "  - REQUIRED variables of local or dependent modules that the user has "
        "    NOT yet supplied a clear value for.\n"
        "  - Answers that are ambiguous, contradictory, or use placeholder words "
        "    like 'TBD', 'maybe', 'either', 'whatever you think'.\n"
        "  - Critical decisions still missing (e.g. region not specified, "
        "    encryption posture unclear, IAM principal not named).\n\n"
        f"Return at MOST {MAX_FOLLOWUPS} follow-up questions as a JSON array of strings. "
        "If the answers fully cover everything needed, return an empty array []. "
        "Output ONLY the JSON array — no markdown, no commentary."
    )

    try:
        resp = await client.chat.completions.create(
            model=cfg.llm.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=400,
        )
        raw = _strip_fences(resp.choices[0].message.content.strip())
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            cleaned = [q.strip() for q in parsed if isinstance(q, str) and q.strip()]
            # Filter out questions we've already asked verbatim
            asked = {qa.question for qa in session.qa_pairs}
            new = [q for q in cleaned if q not in asked]
            return new[:MAX_FOLLOWUPS]
    except Exception as exc:
        print(f"[question_engine] LLM follow-up generation failed: {exc}")

    # Deterministic fallback: detect placeholder answers
    return _heuristic_followups(session)


_PLACEHOLDER_PATTERNS = re.compile(
    r"\b(tbd|tba|to be (decided|determined)|don'?t know|unsure|maybe|"
    r"either|whatever|n/?a|none|skip|later)\b",
    re.IGNORECASE,
)


def _heuristic_followups(session: CurationSession) -> list[str]:
    """If LLM failed, scan answers for obvious placeholders and re-ask."""
    followups: list[str] = []
    for qa in session.qa_pairs:
        if _PLACEHOLDER_PATTERNS.search(qa.answer):
            followups.append(
                f"You answered '{qa.answer[:40]}…' to '{qa.question[:80]}'. "
                "Could you give a concrete value?"
            )
        if len(followups) >= MAX_FOLLOWUPS:
            break
    return followups


# ── Helpers ──────────────────────────────────────────────────────────────────

def _strip_fences(text: str) -> str:
    """Remove markdown code fences if the LLM wrapped the JSON."""
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = text.replace("```", "").strip()
    return text


def _fallback_questions(session: CurationSession) -> list[str]:
    service = session.service_name or "this service"
    mode    = session.mode
    prov    = session.provider.value  # "google" | "aws" | "azurerm"

    if mode == CurationMode.SELF_CURATION:
        return [
            f"What specific changes or new features should the new version of the {service} module include?",
            "Which existing resources should be modified, and which should remain unchanged?",
            "Are there any breaking changes that downstream module consumers must be aware of?",
            "What is the new semantic version tag (e.g. v2.1.0) and what drove the version bump?",
            "Should the module maintain backward-compatible variable defaults or is a clean break acceptable?",
            "Which environments (dev / staging / production) need to be validated before tagging?",
            "Are there any new IAM roles, permissions, or compliance controls required in this version?",
        ]

    if mode == CurationMode.FROM_DOCUMENT:
        return [
            f"Which aspects of the specification are most critical for the {service} module to implement first?",
            "Are there environment-specific differences (dev / staging / production) the module must handle?",
            f"What naming conventions and mandatory {prov} labels/tags must every resource carry?",
            f"Which IAM roles, {'service accounts' if prov == 'google' else 'IAM roles/policies' if prov == 'aws' else 'managed identities'} does this service require?",
            "What encryption requirements apply — at-rest (CMEK/KMS/CMK), in-transit (TLS), and key rotation?",
            "Does the service need private endpoints / VPC Service Controls / Private Link, or is public access acceptable?",
            "What monitoring, alerting, and audit-logging must the module configure by default?",
        ]

    # NEW_PRODUCT or FROM_MODULE — provider-specific
    if prov == "google":
        return [
            f"What is the primary workload for the {service} module, and what GCP region(s) should it target?",
            "Should resources be deployed into an existing Shared VPC / host project, or will the module create its own VPC?",
            "Which GCP service accounts and IAM roles does this service need, and should the module create them?",
            "Is CMEK (customer-managed encryption key) required? If so, which Cloud KMS key ring and key name?",
            "Should the module support multi-region deployments or regional failover?",
            "What mandatory resource labels must every GCP resource carry (e.g. cost-center, team, environment)?",
            "Which other GCP services does this module integrate with (e.g. Pub/Sub, Cloud SQL, Artifact Registry, Secret Manager)?",
        ]
    elif prov == "aws":
        return [
            f"What is the primary workload for the {service} module, and which AWS region(s) should it target?",
            "Should resources be placed in an existing VPC with specific subnet IDs, or will the module create networking?",
            "What IAM roles and policies does this service require, and should the module create them with least-privilege?",
            "Is AWS KMS encryption required for at-rest data? If so, should the module create the KMS key or accept an existing key ARN?",
            "Should the module support multi-AZ deployments and auto-scaling? What are the min/max capacity values?",
            "What mandatory resource tags must every AWS resource carry (e.g. CostCenter, Owner, Environment, Project)?",
            "Which other AWS services does this module integrate with (e.g. S3, SQS, RDS, Secrets Manager, CloudWatch)?",
        ]
    else:  # azurerm
        return [
            f"What is the primary workload for the {service} module, and which Azure region(s) should it target?",
            "Should resources be placed in an existing Virtual Network with specific subnet IDs, or will the module create networking?",
            "Which Azure managed identities or service principals does this service require, and should the module create them?",
            "Is Azure Key Vault–based BYOK encryption required? Should the module create the Key Vault or accept an existing URI?",
            "Should the module configure zone-redundancy and availability sets for high availability?",
            "What mandatory Azure resource tags must every resource carry (e.g. CostCenter, Owner, Environment)?",
            "Which other Azure services does this module integrate with (e.g. Azure SQL, Service Bus, Event Hub, Container Registry)?",
        ]
