"""
code_generator.py — Generate Terraform .tf files using the local LLM.

Generation strategy:
  1. Build a rich prompt from all session context (docs, QA, existing code).
  2. Ask the LLM to return files using [FILE: name]...[/FILE] markers.
  3. Parse response in 4 tiers: file markers → JSON → legacy text markers → minimal fallback.
  4. Validate extracted HCL is not JSON garbage or invalid syntax.
  5. Write files to ./output/{service_slug}_{timestamp}/ and return GenerationResult.
  6. For SELF_CURATION mode: also commit and tag the existing git repo.
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
        "  - GCS buckets: uniform_bucket_level_access=true, versioning { enabled=true }",
        "  - Cloud SQL: require_ssl=true, deletion_protection=true, backup enabled",
        "  - Compute: boot disk encrypted (disk_encryption_key or CMEK variable)",
        "  - IAM: google_project_iam_member not google_project_iam_policy (no binding overwrites)",
        "  - Audit logs: enable where supported (google_project_iam_audit_config)",
    ],
    "aws": [
        "  - S3: block_public_acls=true, block_public_policy=true, ignore_public_acls=true, restrict_public_buckets=true",
        "  - EBS/EFS: encrypted=true, kms_key_id from var.kms_key_id",
        "  - RDS: storage_encrypted=true, deletion_protection=true, skip_final_snapshot=false",
        "  - Security groups: no 0.0.0.0/0 ingress on 22/3389/5432/3306/6379; explicit egress",
        "  - IAM: least-privilege inline policies, no wildcard actions/resources",
    ],
    "azurerm": [
        "  - Storage accounts: https_traffic_only_enabled=true, min_tls_version='TLS1_2'",
        "  - SQL: ssl_enforcement_enabled=true, threat_detection_policy enabled",
        "  - Key Vault: soft_delete_retention_days>=7, purge_protection_enabled=true",
        "  - Diagnostic settings enabled for all supported resources",
    ],
}


def _security_rules(provider: str) -> list[str]:
    common = [
        "  - Apply local.common_tags to ALL taggable resources",
        "  - No 0.0.0.0/0 or ::/0 ingress on sensitive ports (22, 3389, 5432, 3306, 6379)",
        "  - TLS / transit encryption enforced where the resource supports it",
        "  - deletion_protection = true on all stateful resources",
    ]
    return common + _PROVIDER_SECURITY_RULES.get(provider, [])


def _build_prompt(session: CurationSession) -> str:
    prov = session.provider.value
    prov_pin = _PROVIDER_VERSION_PINS.get(prov, ">= 4.0")
    svc = session.service_name or "see requirements"

    parts: list[str] = [
        f"You are a senior Terraform engineer. Generate a production-ready {prov} Terraform module.",
        f"Provider: {prov} (pin: {prov_pin})  |  required_version >= 1.9.0",
        f"Service : {svc}  |  Mode: {session.mode.value}",
        "",
    ]

    if session.registry_docs and not session.registry_docs.startswith("["):
        parts += ["## PROVIDER DOCUMENTATION", session.registry_docs[:4000], ""]

    if session.document_text:
        parts += ["## SPECIFICATION", session.document_text[:2500], ""]

    if session.tf_files:
        parts.append("## EXISTING CODE (preserve resource names, extend do not rewrite)")
        for fname, content in list(session.tf_files.items())[:5]:
            parts += [f"-- {fname} --", content[:1800], ""]

    if session.referenced_modules:
        parts.append("## REFERENCED MODULES")
        for src, snippet in list(session.referenced_modules.items())[:3]:
            parts += [f"Source: {src}", snippet[:1200], ""]

    if session.qa_pairs:
        parts.append("## USER REQUIREMENTS")
        for qa in session.qa_pairs:
            parts += [f"Q: {qa.question}", f"A: {qa.answer}"]
        parts.append("")

    parts += [
        "## HCL RULES (enforce strictly)",
        "VARIABLES — full form required:",
        '  variable "name" {',
        '    type        = string           # always explicit',
        '    description = "..."            # always present (terraform-docs)',
        '    default     = value            # omit if required',
        '    sensitive   = false            # set true for secrets',
        '    validation { condition = expr  error_message = "..." }',
        '  }',
        "",
        "OUTPUTS — full form required:",
        '  output "name" { description = "..."  value = ref  sensitive = false }',
        "",
        "LOCALS — define common_tags, apply to all taggable resources:",
        '  locals {',
        '    common_tags = { ManagedBy="terraform" Module="' + svc + '" Environment=var.environment }',
        '  }',
        "  tags = merge(local.common_tags, var.additional_tags)",
        "",
        "NAMING: snake_case only. Resource names: no provider prefix.",
        "  Good: resource 'main'    Bad: resource 'google_bucket_main'",
        "",
        f"VERSION PIN: source='hashicorp/{prov}' version='{prov_pin}'",
        "",
        "FORBIDDEN:",
        "  - Hardcoded account IDs, regions, ARNs, credentials",
        "  - count when for_each is supported",
        "  - Any attribute NOT in the provider schema",
        "  - depends_on unless unavoidable",
        "",
        "## SECURITY DEFAULTS",
        *_security_rules(prov),
        "",
        "## INLINE FLAGS (add as comments where issues exist)",
        "  # ⚠️ SECURITY: <description>",
        "  # 💰 COST: <description>",
        "  # 🔧 TFLINT: <description>",
        "",
        "## OUTPUT FORMAT",
        "Emit FIVE files. Use raw HCL/Markdown between markers — no JSON, no code fences:",
        "",
        "[FILE: main.tf]",
        "<resource blocks + locals with common_tags>",
        "[/FILE]",
        "[FILE: variables.tf]",
        "<all variables with type + description + validation>",
        "[/FILE]",
        "[FILE: outputs.tf]",
        "<all outputs with descriptions>",
        "[/FILE]",
        "[FILE: versions.tf]",
        "<required_version + required_providers only>",
        "[/FILE]",
        "[FILE: README.md]",
        "<one-paragraph description + markdown table: Variable | Type | Default | Description>",
        "[/FILE]",
        "[SUMMARY]<one sentence>[/SUMMARY]",
        "[USAGE]<module call snippet>[/USAGE]",
    ]

    return "\n".join(parts)


# ── LLM call ─────────────────────────────────────────────────────────────────

async def _call_llm(prompt: str) -> str:
    cfg = get_config()
    client = _client()
    max_out = min(4096, cfg.llm.context_window // 2)

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


def _try_parse_json(raw: str) -> Optional[dict]:
    cleaned = _strip_fences(raw)
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    try:
        m = re.search(r"\{[\s\S]+\}", cleaned)
        if m:
            return json.loads(m.group(0))
    except Exception:
        pass
    return None


def _extract_file_markers(raw: str) -> dict[str, str]:
    """Primary parser: extract [FILE: name.tf]...[/FILE] blocks."""
    pattern = re.compile(r"\[FILE:\s*([^\]]+)\]\n?(.*?)\[/FILE\]", re.DOTALL)
    files: dict[str, str] = {}
    for m in pattern.finditer(raw):
        fname = m.group(1).strip()
        content = m.group(2)
        if content.strip():
            files[fname] = content
    return files


def _extract_section(raw: str, name: str) -> str:
    """Extract [SECTION]...[/SECTION] content."""
    pattern = re.compile(rf"\[{re.escape(name)}\]\n?(.*?)\[/{re.escape(name)}\]", re.DOTALL)
    m = pattern.search(raw)
    return m.group(1).strip() if m else ""


def _extract_block(raw: str, filename: str) -> str:
    """Legacy parser: extract content from --- main.tf --- / ## main.tf markers."""
    patterns = [
        re.compile(
            rf"(?:---+|#+|===+)\s*{re.escape(filename)}\s*(?:---+|#+|===+)?(.*?)"
            r"(?=(?:---+|#+|===+)\s*\w+\.tf|\Z)",
            re.DOTALL | re.IGNORECASE,
        )
    ]
    for p in patterns:
        m = p.search(raw)
        if m:
            return _strip_fences(m.group(1).strip())
    return ""


def _is_valid_hcl_content(content: str, filename: str) -> bool:
    """Return False if content is clearly not valid HCL (JSON dump, arrow functions, etc.)."""
    stripped = content.strip()
    if not stripped:
        return False
    # Raw JSON blob dumped as HCL
    if stripped.startswith("{") and '"files"' in stripped and '"main.tf"' in stripped:
        return False
    # JavaScript/Python arrow function syntax (not valid HCL)
    if re.search(r"=\s*\([\w,\s]+\)\s*=>", stripped):
        return False
    if filename == "main.tf":
        return bool(re.search(r"\b(resource|data|module|locals)\b", stripped))
    if filename == "variables.tf":
        return bool(re.search(r'\bvariable\s+"', stripped))
    if filename == "outputs.tf":
        return bool(re.search(r'\boutput\s+"', stripped) or stripped.startswith("#"))
    if filename == "versions.tf":
        return bool(re.search(r"\bterraform\b", stripped))
    return True


def _parse_response(raw: str, session: CurationSession) -> tuple[dict[str, str], str, str]:
    """Returns (files_dict, summary, usage_example).

    Parsing tiers (first match wins):
      1. [FILE: name]...[/FILE] markers  — new primary format, avoids JSON escaping
      2. JSON {files: {...}}             — backward compat with old LLM responses
      3. Legacy text markers             — --- main.tf ---, ## main.tf, etc.
      4. Minimal fallback                — provider-aware stubs
    """
    default_summary = f"Terraform module for {session.service_name}"

    # Tier 1: new file-marker format
    files = _extract_file_markers(raw)
    if files:
        valid = {f: c for f, c in files.items() if _is_valid_hcl_content(c, f)}
        if valid:
            summary = _extract_section(raw, "SUMMARY") or default_summary
            usage = _extract_section(raw, "USAGE") or _usage_from_files(valid)
            return valid, summary, usage

    # Tier 2: JSON format
    data = _try_parse_json(raw)
    if data and "files" in data and isinstance(data["files"], dict):
        files = {k: v for k, v in data["files"].items() if isinstance(v, str)}
        valid = {f: c for f, c in files.items() if _is_valid_hcl_content(c, f)}
        if valid:
            return valid, data.get("summary", default_summary), data.get("usage_example", _usage_from_files(valid))

    # Tier 3: legacy text markers
    files = {}
    for fname in ("main.tf", "variables.tf", "outputs.tf", "versions.tf"):
        block = _extract_block(raw, fname)
        if block and _is_valid_hcl_content(block, fname):
            files[fname] = block
    if files:
        return files, default_summary, _usage_from_files(files)

    # Tier 4: minimal fallback — strip fences but don't dump raw JSON
    main_content = _strip_fences(raw)
    if not _is_valid_hcl_content(main_content, "main.tf"):
        main_content = f"# Code generation failed — review the raw LLM response\n# {session.service_name}\n"
    files = {
        "main.tf": main_content,
        "variables.tf": _minimal_vars(session),
        "outputs.tf": "# No outputs generated\n",
        "versions.tf": _minimal_versions(session),
    }
    return files, default_summary, _usage_from_files(files)


def _minimal_vars(session: CurationSession) -> str:
    pv = session.provider.value
    lines = ['variable "project_id" {\n  description = "Cloud project / account ID"\n  type        = string\n}\n']
    if pv == "google":
        lines.append('variable "region" {\n  description = "GCP region"\n  type        = string\n  default     = "us-central1"\n}\n')
    elif pv == "aws":
        lines.append('variable "region" {\n  description = "AWS region"\n  type        = string\n  default     = "us-east-1"\n}\n')
    elif pv == "azurerm":
        lines.append('variable "location" {\n  description = "Azure location"\n  type        = string\n  default     = "East US"\n}\n')
    return "\n".join(lines)


def _minimal_versions(session: CurationSession) -> str:
    pv = session.provider.value
    ver = {"google": ">= 5.0", "aws": ">= 5.0", "azurerm": ">= 3.0"}.get(pv, ">= 1.0")
    src = f"hashicorp/{pv}"
    return (
        'terraform {\n'
        '  required_version = ">= 1.3"\n'
        '  required_providers {\n'
        f'    {pv} = {{\n'
        f'      source  = "{src}"\n'
        f'      version = "{ver}"\n'
        '    }\n'
        '  }\n'
        '}\n'
    )


def _usage_from_files(files: dict[str, str]) -> str:
    return 'module "example" {\n  source = "./"\n  # Fill in required variables\n}\n'


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
    prompt = _build_prompt(session)
    raw = await _call_llm(prompt)

    files_dict, summary, usage = _parse_response(raw, session)

    # Write to output directory
    out_dir = _output_dir(session.service_name or "module")
    generated: list[GeneratedFile] = []
    for fname, content in files_dict.items():
        file_path = out_dir / fname
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        generated.append(GeneratedFile(filename=fname, content=content))
        print(f"[curator] Written: {file_path}")

    result = GenerationResult(
        files=generated,
        summary=summary or f"Terraform module for {session.service_name}",
        usage_example=usage,
        output_dir=str(out_dir),
    )

    # Validate the generated module (all layers; never raises)
    try:
        from backend.module_curator.validator import validate_curation
        result.validation = await validate_curation(files_dict, out_dir, session)
    except Exception as exc:
        print(f"[code_generator] Validation skipped due to error: {exc}")

    if (
        session.mode == CurationMode.SELF_CURATION
        and session.repo_name
        and session.new_tag
    ):
        ok = await _apply_as_git_tag(session, files_dict)
        result.git_tag_created = ok
        result.git_tag_name = session.new_tag

    return result
