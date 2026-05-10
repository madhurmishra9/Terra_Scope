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

_PROJECT_ROOT = Path(__file__).parent.parent.parent


def _client() -> AsyncOpenAI:
    cfg = get_config()
    return AsyncOpenAI(
        base_url=cfg.llm.base_url.rstrip("/") + "/v1",
        api_key="ollama",
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


def _build_main_prompt(session: CurationSession) -> str:
    """Pass A — generate main.tf only (all resources, locals, data sources)."""
    prov = session.provider.value
    prov_pin = _PROVIDER_VERSION_PINS.get(prov, ">= 4.0")
    svc = session.service_name or "see requirements"

    parts: list[str] = [
        f"You are a senior Terraform engineer. Generate ONLY the main.tf file for a production-ready {prov} Terraform module.",
        f"Provider: {prov} (version pin: {prov_pin})  |  required_terraform_version >= 1.9.0",
        f"Service / Product: {svc}  |  Curation mode: {session.mode.value}",
        "",
    ]

    if session.registry_docs and not session.registry_docs.startswith("["):
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


# ── LLM call ─────────────────────────────────────────────────────────────────

async def _call_llm(prompt: str) -> str:
    cfg = get_config()
    client = _client()
    ctx = getattr(cfg.llm, "context_window", 8192)
    # Each focused pass gets as many tokens as the model allows minus prompt overhead
    max_out = max(3072, min(6144, ctx - 2048))

    resp = await client.chat.completions.create(
        model=cfg.llm.model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=max_out,
    )
    return resp.choices[0].message.content.strip()


# ── Response parsing ──────────────────────────────────────────────────────────

def _strip_fences(text: str) -> str:
    text = re.sub(r"```(?:json|hcl|terraform)?\s*", "", text)
    return text.replace("```", "").strip()




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


# ── Public entry point ────────────────────────────────────────────────────────

async def generate_terraform_code(session: CurationSession) -> GenerationResult:
    prov = session.provider.value
    svc  = session.service_name or "module"
    all_files: dict[str, str] = {}
    summary = f"Terraform {prov} module for {svc}"
    usage   = ""

    # ── Pass A: main.tf ───────────────────────────────────────────────────────
    print(f"[code_generator] Pass A — main.tf ({svc}, {prov})")
    try:
        raw_a   = await _call_llm(_build_main_prompt(session))
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

    if not usage:
        usage = _usage_from_files(all_files, prov)

    # ── Write output files ────────────────────────────────────────────────────
    out_dir = _output_dir(svc)
    generated: list[GeneratedFile] = []
    for fname, content in all_files.items():
        file_path = out_dir / fname
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        generated.append(GeneratedFile(filename=fname, content=content))
        print(f"[curator] Written: {file_path}")

    result = GenerationResult(
        files=generated,
        summary=summary,
        usage_example=usage,
        output_dir=str(out_dir),
    )

    try:
        from backend.module_curator.validator import validate_curation
        result.validation = await validate_curation(all_files, out_dir, session)
    except Exception as exc:
        print(f"[code_generator] Validation skipped: {exc}")

    if session.mode == CurationMode.SELF_CURATION and session.repo_name and session.new_tag:
        ok = await _apply_as_git_tag(session, all_files)
        result.git_tag_created = ok
        result.git_tag_name = session.new_tag

    return result
