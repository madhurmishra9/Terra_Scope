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

MAX_QUESTIONS = 7


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
        f"Generate exactly {MAX_QUESTIONS} clarifying questions to gather everything needed "
        "to write a complete, production-ready Terraform module.\n"
        "Cover ALL of these areas (one question each):\n"
        "  1. Primary workload / use-case and expected traffic or data volume\n"
        "  2. Network topology: VPC/VNet placement, private vs public endpoints, peering requirements\n"
        "  3. IAM / identity: service accounts, roles, least-privilege policies needed\n"
        "  4. Security & compliance: encryption keys (CMEK/KMS/CMK), TLS, audit logging, regulatory constraints\n"
        "  5. High-availability & scaling: multi-region, replicas, auto-scaling thresholds\n"
        "  6. Naming and tagging: conventions, mandatory labels, cost-allocation tags\n"
        "  7. Cross-service integrations: what other services does this module connect to or depend on?\n"
        "Return ONLY a valid JSON array of question strings — no markdown, no extra text.\n"
        'Example: ["Question 1?", "Question 2?"]'
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
            "Should the module support multi-region deployments or regional failover (e.g. dual-region GCS, cross-region read replicas)?",
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
