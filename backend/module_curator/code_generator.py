"""
code_generator.py — Generate complete, production-ready Terraform modules using the local LLM.

Generation strategy (3 focused LLM passes so each file gets a full token budget):
  Pass A → main.tf                            (resources, locals, data sources)
  Pass B → variables.tf + outputs.tf          (derived from Pass A output)
  Pass C → versions.tf + README.md + examples/complete/main.tf + terraform.tfvars.example
Each pass uses [FILE: name]...[/FILE] markers. Missing files fall back to provider-aware stubs.
Final output: 7 files written to ./output/{service_slug}_{timestamp}/
For SELF_CURATION mode: also commit and tag the existing git repo.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from openai import AsyncOpenAI

from backend.config import get_config
from backend.module_curator.models import (
    CurationMode,
    CurationSession,
    GeneratedFile,
    GenerationResult,
)
from backend.module_curator.hcl_splitter import (
    redistribute_module_files,
    terraform_fmt,
)
from backend.module_curator.repair import repair_until_valid
from backend.registry_fetcher.schema_context import (
    build_schema_constraint_block,
    validate_resource_types,
)
from backend.pipeline.models import CurationOutcome
from backend.http_clients import local_client
from backend.llm_options import llm_extra_body, nothink_messages

_PROJECT_ROOT = Path(__file__).parent.parent.parent


def _client() -> AsyncOpenAI:
    cfg = get_config()
    return AsyncOpenAI(
        base_url=cfg.llm.base_url.rstrip("/") + "/v1",
        api_key="ollama",
        # Local Ollama traffic must never be proxied — an AsyncOpenAI client
        # left to its default builds an httpx.AsyncClient with trust_env=True,
        # which routes localhost calls through the corporate HTTP(S)_PROXY.
        http_client=local_client(),
    )


def _output_dir(service_name: str) -> Path:
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", service_name.lower()).strip("_") or "module"
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out = _PROJECT_ROOT / "output" / f"{slug}_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    return out


# ── Prompt builder ────────────────────────────────────────────────────────────

_PROVIDER_VERSION_PINS: dict[str, str] = {
    "google":  "~> 5.40",
    "aws":     "~> 5.70",
    "azurerm": "~> 3.110",
}

_PROVIDER_SECURITY_RULES: dict[str, list[str]] = {
    "google": [
        "  - GCS buckets: uniform_bucket_level_access=true, versioning{enabled=true}",
        "  - Cloud SQL: require_ssl=true, deletion_protection=true, automated backups enabled",
        "  - Compute: boot disk encrypted (disk_encryption_key or CMEK variable)",
        "  - IAM: google_project_iam_member (never google_project_iam_policy — avoids binding overwrites)",
        "  - Audit logs: google_project_iam_audit_config where supported",
        "  - VPC: private_ip_google_access=true on subnets; no 0.0.0.0/0 firewall ingress",
    ],
    "aws": [
        "  - S3: block_public_acls=true, block_public_policy=true, ignore_public_acls=true, restrict_public_buckets=true; versioning enabled",
        "  - EBS/EFS: encrypted=true, kms_key_id=var.kms_key_id",
        "  - RDS: storage_encrypted=true, deletion_protection=true, skip_final_snapshot=false, multi_az=true for prod",
        "  - Security groups: deny 0.0.0.0/0 on ports 22/3389/5432/3306/6379; explicit egress only",
        "  - IAM: least-privilege inline policies, no wildcard actions or resources",
        "  - CloudTrail/CloudWatch Logs enabled for all environments",
    ],
    "azurerm": [
        "  - Storage: https_traffic_only_enabled=true, min_tls_version='TLS1_2', blob soft delete enabled",
        "  - SQL/Flexible Server: ssl_enforcement_enabled=true, threat_detection_policy enabled",
        "  - Key Vault: soft_delete_retention_days>=7, purge_protection_enabled=true, RBAC authorization",
        "  - Diagnostic settings: azurerm_monitor_diagnostic_setting for all supported resources",
        "  - NSG: deny inbound on 22/3389 from Internet; explicit allow rules only",
    ],
}

_PROVIDER_EXAMPLE_REGION: dict[str, str] = {
    "google":  "us-central1",
    "aws":     "us-east-1",
    "azurerm": "East US",
}

_PROVIDER_EXAMPLE_PROJECT: dict[str, str] = {
    "google":  "my-gcp-project-id",
    "aws":     "123456789012",
    "azurerm": "my-azure-subscription-id",
}


def _security_rules(provider: str) -> list[str]:
    common = [
        "  - Apply local.common_tags to ALL taggable resources",
        "  - No 0.0.0.0/0 or ::/0 ingress on sensitive ports (22, 3389, 5432, 3306, 6379)",
        "  - TLS / transit encryption enforced where the resource supports it",
        "  - deletion_protection = true on all stateful resources",
    ]
    return common + _PROVIDER_SECURITY_RULES.get(provider, [])


def _build_main_prompt(session: CurationSession, schema_block: str = "") -> str:
    """Pass A — generate main.tf only (all resources, locals, data sources).

    When `schema_block` is supplied (the authoritative provider schema for the
    planned resource types), it becomes the primary grounding and registry prose
    is trimmed to protect the 8K context window.
    """
    prov = session.provider.value
    prov_pin = _PROVIDER_VERSION_PINS.get(prov, ">= 4.0")
    svc = session.service_name or "see requirements"

    parts: list[str] = [
        f"You are a senior Terraform engineer. Generate ONLY the main.tf file for a production-ready {prov} Terraform module.",
        f"Provider: {prov} (version pin: {prov_pin})  |  required_terraform_version >= 1.9.0",
        f"Service / Product: {svc}  |  Curation mode: {session.mode.value}",
        "",
    ]

    if schema_block:
        parts += [schema_block, ""]
        # Authoritative schema present — keep registry prose short.
        if session.registry_docs and not session.registry_docs.startswith("["):
            parts += ["## PROVIDER DOCS (wiring/examples only)", session.registry_docs[:1500], ""]
    elif session.registry_docs and not session.registry_docs.startswith("["):
        parts += ["## PROVIDER DOCUMENTATION (use these exact resource types and arguments)", session.registry_docs[:5500], ""]

    if session.document_text:
        parts += ["## SPECIFICATION DOCUMENT", session.document_text[:3000], ""]

    if session.tf_files:
        parts.append("## EXISTING CODE (preserve resource names; extend, do not rewrite)")
        for fname, content in list(session.tf_files.items())[:5]:
            parts += [f"-- {fname} --", content[:2000], ""]

    if session.referenced_modules:
        parts.append("## CROSS-REFERENCED MODULES")
        for src, snippet in list(session.referenced_modules.items())[:3]:
            parts += [f"Source: {src}", snippet[:1200], ""]

    # NEW v2.1 — modules discovered in ./repos/ that the generator must PREFER to call
    if session.local_modules:
        parts.append("## LOCAL REPOS (./repos/) — PREFER CALLING THESE OVER RE-IMPLEMENTING")
        parts.append("The following Terraform modules already exist in the local repos/ folder.")
        parts.append("If any of them implements the requirement, the generated main.tf MUST")
        parts.append('call it via a module {} block using source = "../../repos/<name>"')
        parts.append("(do NOT re-implement its resources inline). Map the user's required")
        parts.append("values onto the module's listed required inputs.")
        for m in session.local_modules[:5]:
            res = ", ".join(m.resource_types[:6])
            req = ", ".join(m.required_inputs[:8])
            parts += [
                f"  • {m.name}  →  source = \"../../repos/{m.name}\"",
                f"      path:     {m.rel_path}",
                f"      resources: {res or '(none parsed)'}",
                f"      required:  {req or '(none)'}",
            ]
        parts.append("")

    # NEW v2.1 — transitively-resolved dependent modules
    if session.dependent_modules:
        parts.append("## DEPENDENT MODULES (resolved from existing code)")
        parts.append("These module sources are referenced (directly or transitively) from")
        parts.append("the loaded code. When the generated main.tf calls them, set their")
        parts.append("required inputs to values that match the user's answers below.")
        for d in session.dependent_modules[:6]:
            req = ", ".join(d.required_inputs[:6])
            outs = ", ".join(d.outputs[:6])
            parts += [
                f"  • {d.raw_source}  [{d.kind}, depth {d.depth}]",
                f"      required inputs: {req or '(none)'}",
                f"      outputs:         {outs or '(none)'}",
            ]
        parts.append("")

    if session.qa_pairs:
        parts.append("## USER REQUIREMENTS (implement ALL of these)")
        for qa in session.qa_pairs:
            parts += [f"Q: {qa.question}", f"A: {qa.answer}"]
        parts.append("")

    parts += [
        "## CODING RULES (enforce strictly)",
        "LOCALS block MUST define:",
        f'  name_prefix = "${{var.environment}}-${{var.name}}"',
        f'  common_tags = {{ ManagedBy="terraform" Module="{svc}" Environment=var.environment }}',
        "",
        "RESOURCES:",
        "  - Use var.* for EVERY configurable value — NO hardcoded strings",
        "  - Apply tags = merge(local.common_tags, var.additional_tags) on every taggable resource",
        "  - Use for_each instead of count wherever possible",
        "  - snake_case resource names; no provider prefix in the logical name",
        "  - Add data sources for any external references (IAM, KMS, VPC, subnets, etc.)",
        "  - depends_on only when unavoidable",
        "",
        f"FORBIDDEN: hardcoded account IDs, regions, ARNs, credentials; attributes absent from the {prov} provider schema",
        "",
        "## SECURITY DEFAULTS (non-negotiable)",
        *_security_rules(prov),
        "",
        "## INLINE FLAGS",
        "  # ⚠️ SECURITY: <description>",
        "  # 💰 COST: <description>",
        "",
        "## OUTPUT FORMAT",
        "Output ONLY the block below — raw HCL inside the markers, NO markdown code fences:",
        "",
        "[FILE: main.tf]",
        "# locals block + all data sources + all resource blocks",
        "[/FILE]",
    ]
    return "\n".join(parts)


def _build_vars_prompt(session: CurationSession, main_tf: str) -> str:
    """Pass B — generate variables.tf + outputs.tf derived from main.tf."""
    prov = session.provider.value
    svc = session.service_name or "module"
    region_default = _PROVIDER_EXAMPLE_REGION.get(prov, "us-central1")
    region_var = "location" if prov == "azurerm" else "region"

    parts: list[str] = [
        f"You are a senior Terraform engineer. Given the main.tf below for a {prov} {svc} module,",
        "generate a complete variables.tf AND a complete outputs.tf.",
        "",
        "## GENERATED main.tf (reference — do NOT repeat it in your output)",
        main_tf[:5500],
        "",
    ]

    if session.qa_pairs:
        parts.append("## USER REQUIREMENTS")
        for qa in session.qa_pairs:
            parts += [f"Q: {qa.question}", f"A: {qa.answer}"]
        parts.append("")

    parts += [
        "## VARIABLE RULES",
        "Every variable MUST have: type (always explicit), description (required for terraform-docs).",
        "Use sensitive=true for passwords, tokens, private keys.",
        "Omit default only if the value is truly required from the caller.",
        "Add validation blocks for: region/location format, environment enum, naming patterns, numeric ranges.",
        "",
        f"REQUIRED variables to include: name, environment, {region_var} (default: {region_default!r}), additional_tags (default: {{}})",
        "  Plus all provider-specific identity variables: project_id (GCP) | aws_account_id (AWS) | resource_group_name+location (Azure)",
        "",
        "## OUTPUT RULES",
        "Export ALL of the following that exist in main.tf:",
        "  - Resource IDs, ARNs, self_links",
        "  - Endpoints, hostnames, connection strings (sensitive=true for credentials)",
        "  - Service account emails / managed identity IDs",
        "  - Any value a downstream module would consume",
        "Every output MUST have a description.",
        "",
        "## OUTPUT FORMAT",
        "Output ONLY these two blocks — raw HCL, NO markdown fences:",
        "",
        "[FILE: variables.tf]",
        "# All variables — full form with type + description + validation",
        "[/FILE]",
        "[FILE: outputs.tf]",
        "# All outputs with descriptions; sensitive=true where applicable",
        "[/FILE]",
    ]
    return "\n".join(parts)


def _build_meta_prompt(session: CurationSession, files: dict[str, str]) -> str:
    """Pass C — generate versions.tf, README.md, examples/complete/main.tf, terraform.tfvars.example."""
    prov = session.provider.value
    prov_pin = _PROVIDER_VERSION_PINS.get(prov, ">= 4.0")
    svc = session.service_name or "module"
    example_region  = _PROVIDER_EXAMPLE_REGION.get(prov, "us-central1")
    example_project = _PROVIDER_EXAMPLE_PROJECT.get(prov, "my-project-id")

    vars_content   = files.get("variables.tf", "")[:3500]
    output_content = files.get("outputs.tf", "")[:1500]

    provider_example_block = {
        "google":  f'provider "google" {{\n  project = var.project_id\n  region  = var.region\n}}',
        "aws":     f'provider "aws" {{\n  region = var.region\n}}',
        "azurerm": f'provider "azurerm" {{\n  features {{}}\n}}',
    }.get(prov, f'provider "{prov}" {{}}')

    parts: list[str] = [
        f"You are a senior Terraform engineer. Generate the FOUR support files for a {prov} {svc} Terraform module.",
        "",
        "## variables.tf (for reference)",
        vars_content,
        "",
        "## outputs.tf (for reference)",
        output_content,
        "",
        "## RULES PER FILE",
        "versions.tf:",
        f'  terraform {{ required_version = ">= 1.9.0"  required_providers {{ {prov} = {{ source = "hashicorp/{prov}" version = "{prov_pin}" }} }} }}',
        "",
        "README.md must contain:",
        f"  - H1 title: Terraform {prov} {svc} Module",
        "  - One-paragraph description of what the module provisions",
        "  - ## Usage section with a complete `hcl` module call block",
        "  - ## Requirements table: Terraform version + provider name + version",
        "  - ## Inputs table:  Name | Type | Default | Required | Description",
        "  - ## Outputs table: Name | Description | Sensitive",
        "  - ## License section: Apache 2.0",
        "",
        "examples/complete/main.tf must:",
        f"  - Include: terraform block (required_version, required_providers), {provider_example_block}",
        "  - Call the module with: source = '../../'",
        f"  - Set ALL required variables to realistic values (region={example_region!r}, project/account={example_project!r})",
        "  - Set name='example', environment='dev'",
        "",
        "terraform.tfvars.example:",
        "  - One line per variable with an inline comment explaining valid values",
        "  - Secrets: secret_key = \"<REPLACE_WITH_ACTUAL_SECRET>\"",
        "",
        "## OUTPUT FORMAT",
        "Output ONLY these four blocks — raw HCL/Markdown inside markers, NO markdown code fences:",
        "",
        "[FILE: versions.tf]",
        "# terraform block only",
        "[/FILE]",
        "[FILE: README.md]",
        "# Full module README with all sections listed above",
        "[/FILE]",
        "[FILE: examples/complete/main.tf]",
        "# Complete working example",
        "[/FILE]",
        "[FILE: terraform.tfvars.example]",
        "# Example variable values with comments",
        "[/FILE]",
        "[SUMMARY]One-sentence description of what this module provisions[/SUMMARY]",
        "[USAGE]Complete module call snippet with all required variables filled in[/USAGE]",
    ]
    return "\n".join(parts)


def _build_prompt(session: CurationSession) -> str:
    """Unified single-pass prompt used by tests and legacy callers.
    Wraps _build_main_prompt and appends the full set of output-format markers."""
    base = _build_main_prompt(session)
    extra = "\n".join([
        "[FILE: variables.tf]",
        "# All input variables with type, description, validation",
        "[/FILE]",
        "[FILE: outputs.tf]",
        "# All outputs with descriptions",
        "[/FILE]",
        "[FILE: versions.tf]",
        "# terraform { required_version + required_providers }",
        "[/FILE]",
        "[SUMMARY]One-sentence description of what this module provisions[/SUMMARY]",
        "[USAGE]Complete module call snippet with all required variables filled in[/USAGE]",
    ])
    return base + "\n" + extra


# ── LLM call ─────────────────────────────────────────────────────────────────

async def _call_llm(prompt: str) -> str:
    cfg = get_config()
    client = _client()
    ctx = getattr(cfg.llm, "context_window", 8192)
    # Each focused pass gets as many tokens as the model allows minus prompt overhead
    max_out = max(3072, min(6144, ctx - 2048))

    resp = await client.chat.completions.create(
        model=cfg.llm.model,
        messages=nothink_messages([{"role": "user", "content": prompt}]),
            extra_body=llm_extra_body(),  # disable thinking-mode traces (grounded app)
        temperature=0.1,
        max_tokens=max_out,
    )
    return resp.choices[0].message.content.strip()


# ── Response parsing ──────────────────────────────────────────────────────────

def _strip_fences(text: str) -> str:
    text = re.sub(r"```(?:json|hcl|terraform)?\s*", "", text)
    return text.replace("```", "").strip()


def _try_parse_json(text: str) -> Optional[dict]:
    """Try to parse a JSON dict from raw LLM output; handles fences and embedded JSON."""
    stripped = _strip_fences(text)
    try:
        result = json.loads(stripped)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, ValueError):
        pass
    # Find the first embedded JSON object using raw_decode (doesn't require EOL)
    decoder = json.JSONDecoder()
    idx = stripped.find("{")
    while idx >= 0:
        try:
            result, _ = decoder.raw_decode(stripped, idx)
            if isinstance(result, dict):
                return result
        except (json.JSONDecodeError, ValueError):
            pass
        idx = stripped.find("{", idx + 1)
    return None


def _extract_file_markers(raw: str) -> dict[str, str]:
    """Extract [FILE: name]...[/FILE] blocks; strip any code fences inside them."""
    pattern = re.compile(r"\[FILE:\s*([^\]]+)\]\n?(.*?)\[/FILE\]", re.DOTALL)
    files: dict[str, str] = {}
    for m in pattern.finditer(raw):
        fname   = m.group(1).strip()
        content = _strip_fences(m.group(2))
        if content.strip():
            files[fname] = content
    return files


def _extract_section(raw: str, name: str) -> str:
    """Extract [SECTION]...[/SECTION] content."""
    pattern = re.compile(rf"\[{re.escape(name)}\]\n?(.*?)\[/{re.escape(name)}\]", re.DOTALL)
    m = pattern.search(raw)
    return m.group(1).strip() if m else ""


def _extract_block(raw: str, filename: str) -> str:
    """Extract content between legacy '--- filename ---' separator markers."""
    escaped = re.escape(filename)
    pattern = re.compile(rf"---\s*{escaped}\s*---\n?(.*?)(?=---|\Z)", re.DOTALL)
    m = pattern.search(raw)
    return m.group(1).strip() if m else ""


def _is_valid_hcl_content(content: str, filename: str) -> bool:
    """Return False if content is clearly not valid HCL (JSON dump, arrow functions, etc.)."""
    stripped = content.strip()
    if not stripped:
        return False
    if stripped.startswith("{") and '"files"' in stripped and '"main.tf"' in stripped:
        return False
    if re.search(r"=\s*\([\w,\s]+\)\s*=>", stripped):
        return False
    base = Path(filename).name  # handle subdirs like examples/complete/main.tf
    if base == "main.tf":
        return bool(re.search(r"\b(resource|data|module|locals|terraform)\b", stripped))
    if base == "variables.tf":
        return bool(re.search(r'\bvariable\s+"', stripped))
    if base == "outputs.tf":
        return bool(re.search(r'\boutput\s+"', stripped) or stripped.startswith("#"))
    if base == "versions.tf":
        return bool(re.search(r"\bterraform\b", stripped))
    return True  # README.md, .tfvars.example, etc.




def _minimal_vars(session: CurationSession) -> str:
    pv = session.provider.value
    region_var = "location" if pv == "azurerm" else "region"
    region_default = _PROVIDER_EXAMPLE_REGION.get(pv, "us-central1")

    base = [
        'variable "name" {',
        f'  description = "Base name for {session.service_name or "module"} resources"',
        '  type        = string',
        '  validation {',
        '    condition     = can(regex("^[a-z][a-z0-9-]{2,28}[a-z0-9]$", var.name))',
        '    error_message = "name must be 4-30 lowercase alphanumeric characters or hyphens."',
        '  }',
        '}',
        '',
        'variable "environment" {',
        '  description = "Deployment environment"',
        '  type        = string',
        '  default     = "dev"',
        '  validation {',
        '    condition     = contains(["dev", "staging", "production"], var.environment)',
        '    error_message = "environment must be dev, staging, or production."',
        '  }',
        '}',
        '',
    ]

    if pv == "google":
        base += ['variable "project_id" {', '  description = "GCP project ID"', '  type        = string', '}', '']
    elif pv == "aws":
        base += ['variable "aws_account_id" {', '  description = "AWS account ID"', '  type        = string', '}', '']
    elif pv == "azurerm":
        base += [
            'variable "resource_group_name" {', '  description = "Azure resource group name"', '  type        = string', '}', '',
            'variable "resource_group_location" {', '  description = "Azure resource group location"', '  type        = string', '}', '',
        ]

    base += [
        f'variable "{region_var}" {{',
        f'  description = "Deployment region / location"',
        '  type        = string',
        f'  default     = "{region_default}"',
        '}',
        '',
        'variable "additional_tags" {',
        '  description = "Extra tags/labels merged onto all taggable resources"',
        '  type        = map(string)',
        '  default     = {}',
        '}',
    ]
    return "\n".join(base)


def _minimal_outputs(session: CurationSession) -> str:
    svc = re.sub(r"[^a-z0-9]+", "_", (session.service_name or "resource").lower()).strip("_")
    return (
        f'output "{svc}_id" {{\n'
        f'  description = "The ID of the {session.service_name or "resource"}"\n'
        f'  value       = null  # TODO: replace with actual resource reference\n'
        '}\n'
    )


def _minimal_versions(session: CurationSession) -> str:
    pv = session.provider.value
    pin = _PROVIDER_VERSION_PINS.get(pv, ">= 1.0")
    return (
        'terraform {\n'
        '  required_version = ">= 1.9.0"\n'
        '  required_providers {\n'
        f'    {pv} = {{\n'
        f'      source  = "hashicorp/{pv}"\n'
        f'      version = "{pin}"\n'
        '    }\n'
        '  }\n'
        '}\n'
    )


def _minimal_example(session: CurationSession) -> str:
    pv = session.provider.value
    pin = _PROVIDER_VERSION_PINS.get(pv, ">= 1.0")
    region = _PROVIDER_EXAMPLE_REGION.get(pv, "us-central1")
    project = _PROVIDER_EXAMPLE_PROJECT.get(pv, "my-project-id")
    svc_slug = re.sub(r"\s+", "_", (session.service_name or "module").lower())

    provider_block = {
        "google":  f'provider "google" {{\n  project = "{project}"\n  region  = "{region}"\n}}',
        "aws":     f'provider "aws" {{\n  region = "{region}"\n}}',
        "azurerm": 'provider "azurerm" {\n  features {}\n}',
    }.get(pv, f'provider "{pv}" {{}}')

    id_var = {"google": f'project_id = "{project}"', "aws": f'aws_account_id = "{project}"',
              "azurerm": 'resource_group_name     = "example-rg"\n  resource_group_location = "East US"'}.get(pv, "")

    return (
        'terraform {\n'
        '  required_version = ">= 1.9.0"\n'
        '  required_providers {\n'
        f'    {pv} = {{\n'
        f'      source  = "hashicorp/{pv}"\n'
        f'      version = "{pin}"\n'
        '    }\n'
        '  }\n'
        '}\n\n'
        f'{provider_block}\n\n'
        f'module "{svc_slug}" {{\n'
        '  source = "../../"\n\n'
        '  name        = "example"\n'
        '  environment = "dev"\n'
        f'  {id_var}\n'
        '}\n'
    )


def _minimal_tfvars(session: CurationSession) -> str:
    pv = session.provider.value
    region = _PROVIDER_EXAMPLE_REGION.get(pv, "us-central1")
    lines = [
        "# terraform.tfvars.example — copy to terraform.tfvars and fill in real values",
        "",
        'name        = "my-module"    # 4-30 lowercase alphanumeric chars or hyphens',
        'environment = "dev"          # dev | staging | production',
    ]
    if pv == "google":
        lines += [f'project_id  = "my-gcp-project"', f'region      = "{region}"']
    elif pv == "aws":
        lines += [f'aws_account_id = "123456789012"', f'region         = "{region}"']
    elif pv == "azurerm":
        lines += ['resource_group_name     = "my-rg"', 'resource_group_location = "East US"']
    lines += [
        "",
        "additional_tags = {",
        '  CostCenter = "engineering"',
        '  Owner      = "platform-team"',
        "}",
    ]
    return "\n".join(lines)


def _mock_value_for_type(type_str: str) -> str:
    """A safe HCL literal for a variable's declared type — used to fill
    required variables in the deterministic smoke test so `terraform test
    -command=plan` can resolve the graph without real credentials."""
    t = (type_str or "").strip().lower().removeprefix("${").removesuffix("}")
    if t.startswith("bool"):
        return "true"
    if t.startswith("number"):
        return "1"
    if t.startswith(("list", "set", "tuple")):
        return "[]"
    if t.startswith(("map", "object")):
        return "{}"
    return '"test-value"'


def _minimal_tf_test(session: CurationSession, all_files: dict[str, str]) -> str:
    """Priority 5: a minimal `tests/*.tftest.hcl` smoke test, generated
    deterministically (no LLM call) from the module's own variables/outputs.
    `command = plan` + a mocked provider — fast, no credentials, works in a
    local dev setup. A sandbox-project `apply` tier is a later, opt-in gate.
    """
    from backend.module_curator.docgen.metadata.extractor import extract_from_files

    meta = extract_from_files(all_files, service_name=session.service_name)
    prov = session.provider.value

    def _bare(name: str) -> str:
        # extract_from_files' hcl2 string-mode parse can leave the block
        # label's surrounding quotes attached to the key — strip them so we
        # always emit a bare HCL identifier.
        return name.strip().strip('"')

    lines = [
        f'mock_provider "{prov}" {{}}',
        "",
        'run "plan_defaults" {',
        "  command = plan",
    ]
    if meta.required_inputs:
        lines.append("")
        lines.append("  variables {")
        for name, iv in meta.required_inputs.items():
            lines.append(f"    {_bare(name)} = {_mock_value_for_type(iv.type)}")
        lines.append("  }")

    # 2-3 assertions on outputs, if the module declares any.
    asserted = [_bare(n) for n in list(meta.outputs.keys())[:3]]
    if asserted:
        lines.append("")
        for name in asserted:
            lines.append("  assert {")
            lines.append(f"    condition     = output.{name} != null")
            lines.append(f'    error_message = "{name} output must be set"')
            lines.append("  }")

    lines.append("}")
    return "\n".join(lines) + "\n"


def _parse_response(
    raw: str, session: CurationSession
) -> tuple[dict[str, str], str, str]:
    """4-tier response parser → (files, summary, usage).

    Tier 1: [FILE: name]...[/FILE] markers
    Tier 2: JSON {"files": {...}, "summary": "...", "usage_example": "..."}
    Tier 3: Legacy --- name --- separator markers
    Tier 4: Provider-aware minimal fallback stubs
    """
    prov = session.provider.value
    svc = session.service_name or "module"

    def _fill_missing(files: dict[str, str]) -> None:
        if "variables.tf" not in files:
            files["variables.tf"] = _minimal_vars(session)
        if "outputs.tf" not in files:
            files["outputs.tf"] = _minimal_outputs(session)
        if "versions.tf" not in files:
            files["versions.tf"] = _minimal_versions(session)

    # Tier 1 — [FILE: name]...[/FILE] markers
    t1 = _extract_file_markers(raw)
    if t1:
        t1 = {k: v for k, v in t1.items() if _is_valid_hcl_content(v, k)}
        if t1:
            summary = _extract_section(raw, "SUMMARY")
            usage = _extract_section(raw, "USAGE")
            return t1, summary, usage

    # Tier 2 — JSON {"files": {...}}
    parsed = _try_parse_json(raw)
    if parsed and "files" in parsed and isinstance(parsed["files"], dict):
        t2 = {
            k: v for k, v in parsed["files"].items()
            if isinstance(v, str) and _is_valid_hcl_content(v, k)
        }
        if t2:
            summary = str(parsed.get("summary", ""))
            usage = str(parsed.get("usage_example", ""))
            return t2, summary, usage

    # Tier 3 — legacy --- filename --- markers
    t3: dict[str, str] = {}
    for fname in ("main.tf", "variables.tf", "outputs.tf", "versions.tf"):
        block = _extract_block(raw, fname)
        if block and _is_valid_hcl_content(block, fname):
            t3[fname] = block
    if t3:
        _fill_missing(t3)
        return t3, f"Terraform module for {svc}", ""

    # Tier 4 — minimal fallback
    t4: dict[str, str] = {
        "main.tf": (
            f"# {prov} {svc} — main.tf (auto-generated stub)\n\n"
            "locals {\n"
            f'  name_prefix = "${{var.environment}}-${{var.name}}"\n'
            "}\n"
        ),
        "variables.tf": _minimal_vars(session),
        "outputs.tf":   _minimal_outputs(session),
        "versions.tf":  _minimal_versions(session),
    }
    return t4, f"Terraform module for {svc}", ""


def _infer_example_value(var_name: str, provider: str = "google") -> str:
    name = var_name.lower()
    if "project" in name:
        return f'"{_PROVIDER_EXAMPLE_PROJECT.get(provider, "my-project-id")}"'
    if "region" in name and provider != "azurerm":
        return f'"{_PROVIDER_EXAMPLE_REGION.get(provider, "us-central1")}"'
    if "location" in name:
        return f'"{_PROVIDER_EXAMPLE_REGION.get(provider, "East US")}"'
    if name in ("environment", "env"):
        return '"production"'
    if "name" in name and "domain" not in name:
        return '"my-module"'
    if "bucket" in name:
        return '"my-app-bucket"'
    if "zone" in name:
        return '"us-central1-a"'
    if "cidr" in name or ("ip" in name and "range" in name):
        return '"10.0.0.0/16"'
    if "port" in name:
        return "8080"
    if any(x in name for x in ("count", "min_", "max_", "size", "capacity")):
        return "2"
    if name.startswith("enable_") or name.endswith("_enabled"):
        return "true"
    if "tags" in name or "labels" in name:
        return "{}"
    if "group" in name and "resource" in name:
        return '"my-resource-group"'
    return '"<replace-me>"'


def _usage_from_files(files: dict[str, str], provider: str = "google") -> str:
    """Build a realistic module call from variables.tf — required vars filled, optional commented."""
    vars_tf = files.get("variables.tf", "")
    required: list[str] = []
    optional_sample: list[str] = []

    for m in re.finditer(r'variable\s+"([^"]+)"\s*\{([^}]+)\}', vars_tf, re.DOTALL):
        var_name, var_body = m.group(1), m.group(2)
        if "default" not in var_body:
            required.append(var_name)
        elif len(optional_sample) < 3:
            optional_sample.append(var_name)

    lines = ['module "example" {', '  source = "./"', ""]
    for var in required[:12]:
        lines.append(f"  {var:<30} = {_infer_example_value(var, provider)}")
    if optional_sample:
        lines += ["", "  # Optional — shown with defaults overridden"]
        for var in optional_sample:
            lines.append(f"  # {var:<28} = {_infer_example_value(var, provider)}")
    lines += ["}", ""]
    return "\n".join(lines)


# ── Git tag creation (self-curation) ─────────────────────────────────────────

async def _apply_as_git_tag(
    session: CurationSession,
    files: dict[str, str],
) -> bool:
    from git import Repo, GitCommandError

    cfg = get_config()
    repo_cfg = cfg.get_repo(session.repo_name)  # type: ignore[arg-type]
    if not repo_cfg:
        return False

    repo_path = repo_cfg.resolved_local_path(_PROJECT_ROOT)
    try:
        repo = Repo(str(repo_path))

        # Write only root-level .tf files (avoid sub-module dirs)
        for fname, content in files.items():
            if "/" not in fname and "\\" not in fname:
                (repo_path / fname).write_text(content, encoding="utf-8")
                repo.index.add([fname])

        qa_lines = "\n".join(
            f"  Q: {qa.question[:60]}\n  A: {qa.answer[:80]}"
            for qa in session.qa_pairs[:3]
        )
        msg = (
            f"feat({session.service_name}): TerraScope curation → {session.new_tag}\n\n"
            f"Generated by TerraScope Curator\n{qa_lines}"
        )
        repo.index.commit(msg)
        repo.create_tag(
            session.new_tag,
            message=f"TerraScope generated: {session.new_tag}",
        )
        return True
    except GitCommandError as exc:
        print(f"[code_generator] Git tag failed: {exc}")
        return False
    except Exception as exc:
        print(f"[code_generator] Unexpected git error: {exc}")
        return False


# ── Module-source rewriter (v2.1) ────────────────────────────────────────────

_MODULE_SOURCE_RE = re.compile(r'(source\s*=\s*)"([^"]+)"')


def _rewrite_module_sources_to_local(content: str, session: CurationSession) -> str:
    """
    Walk every `source = "..."` in the generated HCL and, if the source matches
    a module we know about in ./repos/, rewrite it to a stable relative path.

    Why: the LLM often emits registry-style or github URLs even when told to
    prefer local sources. This guarantees the generated code points at the
    local copy the user actually has on disk.
    """
    if not session.local_modules and not session.dependent_modules:
        return content

    try:
        from backend.module_curator.local_repo_scanner import resolve_source_locally
    except Exception:
        return content

    # Build a quick lookup: module name → local rel path
    local_by_name: dict[str, str] = {m.name: m.name for m in session.local_modules}

    def _replace(match: re.Match) -> str:
        prefix, source = match.group(1), match.group(2)
        # Skip already-local sources
        if source.startswith("./") or source.startswith("../"):
            return match.group(0)

        # Try to resolve to a local module
        try:
            local_mod = resolve_source_locally(source)
        except Exception:
            local_mod = None

        if local_mod and local_mod.name in local_by_name:
            new_source = f"../../repos/{local_mod.name}"
            return f'{prefix}"{new_source}"'
        return match.group(0)

    return _MODULE_SOURCE_RE.sub(_replace, content)


def _build_local_modules_used(content: str, session: CurationSession) -> list[str]:
    """Return the names of local modules actually referenced in the final HCL."""
    if not session.local_modules:
        return []
    used: set[str] = set()
    for m in session.local_modules:
        # match either ../../repos/<name> or the original name in source strings
        if f"repos/{m.name}" in content or f'"{m.name}"' in content:
            used.add(m.name)
    return sorted(used)


# ── Typed pass wrappers (consumed by CurationPipeline) ───────────────────────

async def _run_pass_a(session: CurationSession) -> "GenerationPassResult":
    """Run Pass A (main.tf) and return a typed GenerationPassResult."""
    from backend.pipeline.models import GenerationPassResult

    prov = session.provider.value
    svc  = session.service_name or "module"
    print(f"[code_generator] Pass A — main.tf ({svc}, {prov})")
    try:
        raw = await _call_llm(_build_main_prompt(session))
    except Exception as exc:
        print(f"[code_generator] Pass A LLM call failed: {exc}")
        raw = ""

    files = {k: v for k, v in _extract_file_markers(raw).items() if _is_valid_hcl_content(v, k)}
    fallback_used = False
    if "main.tf" not in files:
        fallback_used = True
        files["main.tf"] = (
            f"# {prov} {svc} — main.tf (auto-generated stub)\n\n"
            "locals {\n"
            f'  name_prefix = "${{var.environment}}-${{var.name}}"\n'
            f'  common_tags = {{\n'
            '    ManagedBy   = "terraform"\n'
            f'    Module      = "{svc}"\n'
            '    Environment = var.environment\n'
            '  }\n'
            "}\n"
        )
    return GenerationPassResult(pass_name="A", raw=raw, files=files, fallback_used=fallback_used)


async def _run_pass_b(session: CurationSession, main_tf_raw: str) -> "GenerationPassResult":
    """Run Pass B (variables.tf + outputs.tf) and return a typed GenerationPassResult."""
    from backend.pipeline.models import GenerationPassResult

    main_tf_content = _extract_file_markers(main_tf_raw).get("main.tf", main_tf_raw[:5500])
    print(f"[code_generator] Pass B — variables.tf + outputs.tf")
    try:
        raw = await _call_llm(_build_vars_prompt(session, main_tf_content))
    except Exception as exc:
        print(f"[code_generator] Pass B LLM call failed: {exc}")
        raw = ""

    files = {k: v for k, v in _extract_file_markers(raw).items() if _is_valid_hcl_content(v, k)}
    fallback_used = False
    if "variables.tf" not in files:
        fallback_used = True
        files["variables.tf"] = _minimal_vars(session)
    if "outputs.tf" not in files:
        fallback_used = True
        files["outputs.tf"] = _minimal_outputs(session)
    return GenerationPassResult(pass_name="B", raw=raw, files=files, fallback_used=fallback_used)


async def _run_pass_c(session: CurationSession, all_files: dict[str, str]) -> "GenerationPassResult":
    """Run Pass C (versions.tf + README + examples) and return a typed GenerationPassResult."""
    from backend.pipeline.models import GenerationPassResult

    prov = session.provider.value
    svc  = session.service_name or "module"
    print(f"[code_generator] Pass C — versions.tf, README.md, examples")
    try:
        raw = await _call_llm(_build_meta_prompt(session, all_files))
    except Exception as exc:
        print(f"[code_generator] Pass C LLM call failed: {exc}")
        raw = ""

    files = {k: v for k, v in _extract_file_markers(raw).items() if _is_valid_hcl_content(v, k)}
    fallback_used = False
    if "versions.tf" not in files:
        fallback_used = True
        files["versions.tf"] = _minimal_versions(session)
    if "README.md" not in files:
        fallback_used = True
        files["README.md"] = (
            f"# Terraform {prov} {svc} Module\n\n"
            f"Production-ready {prov} Terraform module for **{svc}**.\n\n"
            f"## Usage\n\n```hcl\n{_usage_from_files(all_files, prov)}\n```\n"
        )
    if "examples/complete/main.tf" not in files:
        fallback_used = True
        files["examples/complete/main.tf"] = _minimal_example(session)
    if "terraform.tfvars.example" not in files:
        fallback_used = True
        files["terraform.tfvars.example"] = _minimal_tfvars(session)
    return GenerationPassResult(pass_name="C", raw=raw, files=files, fallback_used=fallback_used)


# ── Schema grounding: plan resource types, then fetch their schema ───────────

async def _plan_resource_types(session: CurationSession) -> list[str]:
    """Ask the model which resource types the module needs, then keep only the
    ones that actually exist in the provider schema.

    This is a tiny, cheap pass whose output we sanitise against the schema, so
    the expensive Pass A never anchors on a hallucinated resource type.
    """
    prov = session.provider.value
    ctx_lines: list[str] = []
    if session.registry_docs and not session.registry_docs.startswith("["):
        ctx_lines.append(session.registry_docs[:1500])
    for qa in session.qa_pairs[:8]:
        ctx_lines.append(f"Q: {qa.question}\nA: {qa.answer}")
    if session.document_text:
        ctx_lines.append(session.document_text[:1200])

    prompt = "\n".join([
        f"For a {prov} Terraform module for '{session.service_name or 'the requirements below'}',",
        "list the EXACT provider resource types it needs "
        f"(e.g. google_storage_bucket). Provider prefix is '{prov}_'.",
        "Output ONLY a JSON array of resource-type strings, nothing else.",
        "",
        "## CONTEXT",
        *ctx_lines,
    ])

    try:
        raw = await _call_llm(prompt)
    except Exception as exc:
        print(f"[code_generator] Resource-type planning failed: {exc}")
        return []

    stripped = _strip_fences(raw)
    candidates: list[str] = []
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, list):
        candidates = [str(x) for x in parsed]
    elif isinstance(parsed, dict):
        for v in parsed.values():
            if isinstance(v, list):
                candidates = [str(x) for x in v]
                break
    if not candidates:
        # last resort: regex out provider_-prefixed identifiers (incl. embedded)
        candidates = re.findall(rf"\b{re.escape(prov)}_[a-z0-9_]+\b", raw)

    validated = await validate_resource_types(prov, candidates)
    print(f"[code_generator] Planned resource types ({len(validated)}): {validated}")
    return validated


# ── Public entry point ────────────────────────────────────────────────────────

async def generate_terraform_code(session: CurationSession) -> GenerationResult:
    prov = session.provider.value
    svc  = session.service_name or "module"
    all_files: dict[str, str] = {}
    summary = f"Terraform {prov} module for {svc}"
    usage   = ""

    # ── Schema grounding (plan resource types → fetch authoritative schema) ────
    schema_block = ""
    try:
        planned_types = await _plan_resource_types(session)
        schema_block = await build_schema_constraint_block(prov, planned_types)
    except Exception as exc:
        print(f"[code_generator] Schema grounding skipped: {exc}")

    # ── Pass A: main.tf ───────────────────────────────────────────────────────
    print(f"[code_generator] Pass A — main.tf ({svc}, {prov})")
    try:
        raw_a   = await _call_llm(_build_main_prompt(session, schema_block))
        files_a = _extract_file_markers(raw_a)
        for fname, content in files_a.items():
            if _is_valid_hcl_content(content, fname):
                all_files[fname] = content
    except Exception as exc:
        print(f"[code_generator] Pass A failed: {exc}")

    if "main.tf" not in all_files:
        print("[code_generator] Pass A: no valid main.tf — using minimal stub")
        all_files["main.tf"] = (
            f"# {prov} {svc} — main.tf (auto-generated stub)\n\n"
            "locals {\n"
            f'  name_prefix = "${{var.environment}}-${{var.name}}"\n'
            f'  common_tags = {{\n'
            '    ManagedBy   = "terraform"\n'
            f'    Module      = "{svc}"\n'
            '    Environment = var.environment\n'
            '  }\n'
            "}\n"
        )

    # ── Pass B: variables.tf + outputs.tf ────────────────────────────────────
    print(f"[code_generator] Pass B — variables.tf + outputs.tf ({svc}, {prov})")
    try:
        raw_b   = await _call_llm(_build_vars_prompt(session, all_files["main.tf"]))
        files_b = _extract_file_markers(raw_b)
        for fname, content in files_b.items():
            if fname not in all_files and _is_valid_hcl_content(content, fname):
                all_files[fname] = content
    except Exception as exc:
        print(f"[code_generator] Pass B failed: {exc}")

    if "variables.tf" not in all_files:
        all_files["variables.tf"] = _minimal_vars(session)
    if "outputs.tf" not in all_files:
        all_files["outputs.tf"] = _minimal_outputs(session)

    # ── Pass C: versions.tf + README + examples ───────────────────────────────
    print(f"[code_generator] Pass C — versions.tf, README.md, examples ({svc}, {prov})")
    try:
        raw_c   = await _call_llm(_build_meta_prompt(session, all_files))
        files_c = _extract_file_markers(raw_c)
        for fname, content in files_c.items():
            if fname not in all_files and _is_valid_hcl_content(content, fname):
                all_files[fname] = content
        summary = _extract_section(raw_c, "SUMMARY") or summary
        usage   = _extract_section(raw_c, "USAGE")
    except Exception as exc:
        print(f"[code_generator] Pass C failed: {exc}")

    # Fill any still-missing files with provider-aware stubs
    if "versions.tf" not in all_files:
        all_files["versions.tf"] = _minimal_versions(session)
    if "README.md" not in all_files:
        all_files["README.md"] = (
            f"# Terraform {prov} {svc} Module\n\n"
            f"Production-ready {prov} Terraform module for **{svc}**.\n\n"
            f"## Usage\n\n```hcl\n{_usage_from_files(all_files, prov)}\n```\n"
        )
    if "examples/complete/main.tf" not in all_files:
        all_files["examples/complete/main.tf"] = _minimal_example(session)
    if "terraform.tfvars.example" not in all_files:
        all_files["terraform.tfvars.example"] = _minimal_tfvars(session)
    if not any(f.endswith(".tftest.hcl") for f in all_files):
        try:
            all_files["tests/defaults.tftest.hcl"] = _minimal_tf_test(session, all_files)
        except Exception as exc:
            print(f"[code_generator] Smoke test generation skipped: {exc}")

    if not usage:
        usage = _usage_from_files(all_files, prov)

    # ── v2.1: rewrite module sources to point at ./repos/ where we have a local copy ──
    for fname in list(all_files.keys()):
        if fname.endswith(".tf"):
            try:
                all_files[fname] = _rewrite_module_sources_to_local(all_files[fname], session)
            except Exception as exc:
                print(f"[code_generator] Source rewrite skipped for {fname}: {exc}")

    # ── Deterministic modular layout: split into variables/locals/data/... ─────
    all_files = redistribute_module_files(all_files)
    all_files = terraform_fmt(all_files)

    # ── Embed an architecture diagram (mermaid, from real HCL) into the README ──
    try:
        from backend.module_curator.docgen.diagrams.hcl_graph import graph_from_hcl
        from backend.module_curator.docgen.diagrams.readme_embed import (
            diagram_section, upsert_diagram_section,
        )
        graph = graph_from_hcl(all_files)
        if graph.nodes:
            section = diagram_section(graph.to_mermaid(), mode="mermaid", title="Architecture")
            readme = all_files.get("README.md", f"# {svc}\n")
            all_files["README.md"] = upsert_diagram_section(readme, section)
    except Exception as exc:
        print(f"[code_generator] README diagram embed skipped: {exc}")

    out_dir = _output_dir(svc)

    # ── Validate → repair loop (schema + terraform validate + tflint) ──────────
    from backend.module_curator.validator import validate_curation
    validation = None
    outcome = CurationOutcome.CANDIDATE_READY
    outstanding_issues: list[str] = []
    try:
        all_files, validation = await repair_until_valid(
            all_files, out_dir, session,
            call_llm=_call_llm,
            validate_fn=validate_curation,
            split_fn=redistribute_module_files,
            fmt_fn=terraform_fmt,
            max_rounds=3,
        )
        if validation.passed:
            outcome = CurationOutcome.CANDIDATE_READY
        else:
            outcome = CurationOutcome.ESCALATED
            outstanding_issues = [
                f"{i.file or 'main.tf'}: {i.message}"
                for i in validation.issues if i.severity == "error"
            ]
    except Exception as exc:
        print(f"[code_generator] Repair loop skipped: {exc}")
        outcome = CurationOutcome.FAILED
        outstanding_issues = [str(exc)]
        for fname, content in all_files.items():
            fp = out_dir / fname
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")

    # Escalated/failed output can never be mistaken for approved output — same
    # trick as the doc pipeline's `.ESCALATED.md` suffix.
    if outcome is not CurationOutcome.CANDIDATE_READY:
        escalated_dir = out_dir.with_name(out_dir.name + "-ESCALATED")
        try:
            out_dir.rename(escalated_dir)
            out_dir = escalated_dir
        except OSError as exc:
            print(f"[code_generator] Could not rename to ESCALATED dir: {exc}")

    generated: list[GeneratedFile] = [
        GeneratedFile(filename=f, content=c) for f, c in all_files.items()
    ]
    for f in all_files:
        print(f"[curator] Written: {out_dir / f}")

    result = GenerationResult(
        files=generated,
        summary=summary,
        usage_example=usage,
        output_dir=str(out_dir),
        validation=validation,
        outcome=outcome,
        outstanding_issues=outstanding_issues,
    )

    if session.mode == CurationMode.SELF_CURATION and session.repo_name and session.new_tag:
        ok = await _apply_as_git_tag(session, all_files)
        result.git_tag_created = ok
        result.git_tag_name = session.new_tag

    return result
