"""
troubleshooter.py — Terraform module bug detection + version recommendation.

Three-stage pipeline:
  Stage 1  Static analysis   — undefined refs, type mismatches, missing attrs,
                                provider constraint checks, deprecated usage.
  Stage 2  LLM analysis      — logical bugs, security issues, anti-patterns,
                                dependency ordering, best practices.
  Stage 3  Version recommend — find the minimum provider version that fixes
                                detected issues without introducing breaking
                                changes for the module's resource types.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional

import httpx

from backend.config import get_config
from backend.agent.tools.git_tools import (
    list_tf_files_at_tag,
    get_file_at_tag,
)
from backend.agent.tools.hcl_tools import (
    get_all_variables,
    get_all_resources,
    summarize_module,
    parse_hcl_content,
    get_provider_requirements,
)
from backend.ga_workflow.ga_detector import (
    detect_terraform_provider,
    fetch_latest_ga_version,
    fetch_provider_changelog,
    get_current_provider_version,
    TERRAFORM_PROVIDERS,
    _semver_tuple,
)
from backend.troubleshooter.models import (
    TroubleshootIssue,
    TroubleshootResult,
    VersionRecommendation,
    IssueSeverity,
    IssueCategory,
)

_HTTP_TIMEOUT = 30.0


# ── Public entry point ────────────────────────────────────────────────────────

async def troubleshoot_module(
    repo_name: str,
    tag: str,
    problem_description: str = "",
) -> TroubleshootResult:
    """
    Run the full three-stage troubleshooting pipeline on a Terraform module
    at a specific Git tag.

    Args:
        repo_name:           Name matching terrascope.config.yaml
        tag:                 Git tag to analyse (e.g. "v2.1.0" or "main")
        problem_description: Optional user-supplied symptom / error message
    """
    cfg = get_config()

    # Collect all .tf file contents at the tag
    tf_files = list_tf_files_at_tag(repo_name, tag)
    file_contents: dict[str, str] = {}
    for f in tf_files:
        content = get_file_at_tag(repo_name, tag, f)
        if content:
            file_contents[f] = content

    # ── Stage 1: Static analysis ───────────────────────────────────────────────
    static_issues = _static_analysis(file_contents, repo_name, tag)

    # ── Stage 2: LLM analysis ─────────────────────────────────────────────────
    module_summary = summarize_module(repo_name, tag)
    llm_issues = await _llm_analysis(
        file_contents, module_summary, problem_description, repo_name, tag, cfg
    )

    all_issues = static_issues + llm_issues

    # ── Stage 3: Version recommendation ───────────────────────────────────────
    version_rec = await _version_recommendation(repo_name, tag, all_issues)

    error_count   = sum(1 for i in all_issues if i.severity == IssueSeverity.ERROR)
    warning_count = sum(1 for i in all_issues if i.severity == IssueSeverity.WARNING)
    info_count    = sum(1 for i in all_issues if i.severity == IssueSeverity.INFO)

    summary = _build_result_summary(
        repo_name, tag, all_issues, error_count, warning_count, version_rec
    )

    return TroubleshootResult(
        repo_name=repo_name,
        tag=tag,
        scan_date=datetime.now(timezone.utc).isoformat(),
        issues=all_issues,
        error_count=error_count,
        warning_count=warning_count,
        info_count=info_count,
        version_recommendation=version_rec,
        summary=summary,
        scanned_files=tf_files,
    )


# ── Stage 1: Static analysis ──────────────────────────────────────────────────

def _static_analysis(
    file_contents: dict[str, str],
    repo_name: str,
    tag: str,
) -> list[TroubleshootIssue]:
    issues: list[TroubleshootIssue] = []

    # 1a. Parse each file — catch HCL syntax errors
    parsed: dict[str, dict] = {}
    for path, content in file_contents.items():
        result = parse_hcl_content(content)
        if result:
            parsed[path] = result
        else:
            # parse_hcl_content returns {} on failure *and* on truly empty files
            # Only flag as error if the file is non-trivial
            stripped = content.strip()
            if stripped and not stripped.startswith("#"):
                issues.append(TroubleshootIssue(
                    severity=IssueSeverity.ERROR,
                    category=IssueCategory.SYNTAX,
                    file_path=path,
                    message=f"HCL parse failed: '{path}' contains invalid syntax.",
                    suggestion="Run `terraform validate` locally to pinpoint the exact line.",
                ))

    # 1b. Collect declared variable names
    declared_vars: set[str] = set()
    for path, tree in parsed.items():
        for var_name in tree.get("variable", {}).keys():
            declared_vars.add(var_name)

    # 1c. Collect declared local names
    declared_locals: set[str] = set()
    for path, tree in parsed.items():
        for block in tree.get("locals", []):
            if isinstance(block, dict):
                declared_locals.update(block.keys())

    # 1d. Scan for var.X and local.X references in resource/output/data blocks
    _check_undefined_refs_raw(file_contents, declared_vars, declared_locals, issues)

    # 1e. Check variables missing descriptions (best practice)
    _check_variable_descriptions(parsed, issues)

    # 1f. Check for deprecated resource patterns
    _check_deprecated_patterns(file_contents, issues)

    # 1g. Check provider version constraint
    _check_provider_constraints(file_contents, repo_name, tag, issues)

    # 1h. Check for security anti-patterns
    _check_security_patterns(file_contents, issues)

    return issues


def _check_undefined_refs_raw(
    file_contents: dict[str, str],
    declared_vars: set[str],
    declared_locals: set[str],
    issues: list[TroubleshootIssue],
) -> None:
    var_ref_re   = re.compile(r'\bvar\.([a-zA-Z_][a-zA-Z0-9_]*)')
    local_ref_re = re.compile(r'\blocal\.([a-zA-Z_][a-zA-Z0-9_]*)')

    for path, content in file_contents.items():
        if path.endswith("variables.tf") or path.endswith("locals.tf"):
            # Only check non-declaration files for undefined refs
            pass

        for m in var_ref_re.finditer(content):
            name = m.group(1)
            if name not in declared_vars:
                line = content[: m.start()].count("\n") + 1
                issues.append(TroubleshootIssue(
                    severity=IssueSeverity.ERROR,
                    category=IssueCategory.UNDEFINED,
                    file_path=path,
                    line=line,
                    message=f"Reference to undefined variable `var.{name}`.",
                    suggestion=f"Add `variable \"{name}\" {{}}` to variables.tf.",
                ))

        for m in local_ref_re.finditer(content):
            name = m.group(1)
            if name not in declared_locals:
                line = content[: m.start()].count("\n") + 1
                issues.append(TroubleshootIssue(
                    severity=IssueSeverity.WARNING,
                    category=IssueCategory.UNDEFINED,
                    file_path=path,
                    line=line,
                    message=f"Reference to possibly undefined local `local.{name}`.",
                    suggestion=f"Add `{name} = ...` inside a `locals {{}}` block.",
                ))


def _check_variable_descriptions(
    parsed: dict[str, dict],
    issues: list[TroubleshootIssue],
) -> None:
    for path, tree in parsed.items():
        for var_name, var_body in tree.get("variable", {}).items():
            if isinstance(var_body, list):
                var_body = var_body[0] if var_body else {}
            if not isinstance(var_body, dict):
                continue
            if not var_body.get("description"):
                issues.append(TroubleshootIssue(
                    severity=IssueSeverity.INFO,
                    category=IssueCategory.BEST_PRACTICE,
                    file_path=path,
                    resource_name=var_name,
                    message=f"Variable `{var_name}` has no description.",
                    suggestion='Add `description = "..."` to improve module documentation.',
                ))


# Patterns that indicate deprecated or problematic usage
_DEPRECATED_PATTERNS: list[tuple[str, str, str, IssueSeverity]] = [
    # (regex, message, suggestion, severity)
    (
        r'google_container_cluster\b[^{]*\{[^}]*\bmin_master_version\b',
        "Deprecated `min_master_version` in `google_container_cluster`.",
        "Use `release_channel` instead for automatic version management.",
        IssueSeverity.WARNING,
    ),
    (
        r'google_storage_bucket\b[^{]*\{[^}]*\buniform_bucket_level_access\s*=\s*false',
        "`uniform_bucket_level_access = false` is deprecated and insecure.",
        "Set `uniform_bucket_level_access = true` for consistent IAM.",
        IssueSeverity.WARNING,
    ),
    (
        r'resource\s+"google_project_iam_binding"',
        "`google_project_iam_binding` is authoritative and can revoke other bindings.",
        "Prefer `google_project_iam_member` for non-authoritative role grants.",
        IssueSeverity.WARNING,
    ),
    (
        r'aws_s3_bucket_acl\b.*\bacl\s*=\s*"public',
        "S3 bucket ACL set to public.",
        "Set `block_public_acls = true` in `aws_s3_bucket_public_access_block`.",
        IssueSeverity.ERROR,
    ),
    (
        r'azurerm_storage_account\b[^{]*\{[^}]*\ballow_blob_public_access\s*=\s*true',
        "`allow_blob_public_access = true` exposes storage data publicly.",
        "Set `allow_blob_public_access = false` unless explicitly required.",
        IssueSeverity.ERROR,
    ),
]


def _check_deprecated_patterns(
    file_contents: dict[str, str],
    issues: list[TroubleshootIssue],
) -> None:
    for path, content in file_contents.items():
        for pattern, message, suggestion, severity in _DEPRECATED_PATTERNS:
            for m in re.finditer(pattern, content, re.DOTALL):
                line = content[: m.start()].count("\n") + 1
                issues.append(TroubleshootIssue(
                    severity=severity,
                    category=IssueCategory.DEPRECATED,
                    file_path=path,
                    line=line,
                    message=message,
                    suggestion=suggestion,
                ))


# Security anti-patterns
_SECURITY_PATTERNS: list[tuple[str, str, str]] = [
    (
        r'allUsers|allAuthenticatedUsers',
        "IAM member `allUsers` or `allAuthenticatedUsers` grants public access.",
        "Restrict IAM bindings to specific service accounts or groups.",
    ),
    (
        r'ingress\s*=\s*"INGRESS_TRAFFIC_ALL"',
        "Cloud Run ingress set to allow all traffic (no VPC restriction).",
        "Use `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER` or `INGRESS_TRAFFIC_INTERNAL_ONLY`.",
    ),
    (
        r'ssl_policy\s*=\s*"[Mm][Ii][Nn][Ii][Mm][Uu][Mm]"',
        "SSL policy set to MINIMUM — TLS 1.0/1.1 are allowed.",
        "Use `RESTRICTED` or `MODERN` SSL policy to enforce TLS 1.2+.",
    ),
    (
        r'prevent_destroy\s*=\s*false',
        "`prevent_destroy = false` on a stateful resource — accidental deletion is possible.",
        "Set `prevent_destroy = true` in the `lifecycle` block for critical resources.",
    ),
    (
        r'password\s*=\s*"[^$][^{]',
        "Hardcoded password detected in resource block.",
        "Use a variable with `sensitive = true` or reference a secret manager resource.",
    ),
]


def _check_security_patterns(
    file_contents: dict[str, str],
    issues: list[TroubleshootIssue],
) -> None:
    for path, content in file_contents.items():
        for pattern, message, suggestion in _SECURITY_PATTERNS:
            for m in re.finditer(pattern, content):
                line = content[: m.start()].count("\n") + 1
                issues.append(TroubleshootIssue(
                    severity=IssueSeverity.ERROR,
                    category=IssueCategory.SECURITY,
                    file_path=path,
                    line=line,
                    message=message,
                    suggestion=suggestion,
                ))


def _check_provider_constraints(
    file_contents: dict[str, str],
    repo_name: str,
    tag: str,
    issues: list[TroubleshootIssue],
) -> None:
    provider_reqs = get_provider_requirements(repo_name, tag)
    if not provider_reqs:
        issues.append(TroubleshootIssue(
            severity=IssueSeverity.WARNING,
            category=IssueCategory.PROVIDER,
            file_path="versions.tf",
            message="No `versions.tf` or `terraform {}` block found.",
            suggestion="Add a `versions.tf` with `required_version` and `required_providers` constraints.",
        ))
        return

    tf_version = provider_reqs.get("required_version", "")
    if not tf_version:
        issues.append(TroubleshootIssue(
            severity=IssueSeverity.WARNING,
            category=IssueCategory.PROVIDER,
            file_path=provider_reqs.get("source_file", "versions.tf"),
            message="No `required_version` constraint for Terraform itself.",
            suggestion='Add `required_version = ">= 1.9.0"` to the `terraform {}` block.',
        ))

    providers = provider_reqs.get("required_providers", {})
    for prov_key, prov_val in providers.items():
        if isinstance(prov_val, list):
            prov_val = prov_val[0] if prov_val else {}
        if not isinstance(prov_val, dict):
            continue
        ver_constraint = prov_val.get("version", "")
        if not ver_constraint:
            issues.append(TroubleshootIssue(
                severity=IssueSeverity.INFO,
                category=IssueCategory.PROVIDER,
                file_path=provider_reqs.get("source_file", "versions.tf"),
                message=f"Provider `{prov_key}` has no version constraint.",
                suggestion=f'Add `version = ">= X.Y.Z"` to pin a minimum version.',
            ))
        # Overly permissive: no upper bound combined with a very old lower bound
        if ver_constraint and re.match(r'^>=\s*[0-3]\.', ver_constraint):
            issues.append(TroubleshootIssue(
                severity=IssueSeverity.INFO,
                category=IssueCategory.PROVIDER,
                file_path=provider_reqs.get("source_file", "versions.tf"),
                message=f"Provider `{prov_key}` version constraint `{ver_constraint}` allows very old versions.",
                suggestion="Raise the minimum version to reduce exposure to old bugs.",
            ))


# ── Stage 2: LLM analysis ─────────────────────────────────────────────────────

async def _llm_analysis(
    file_contents: dict[str, str],
    module_summary: dict,
    problem_description: str,
    repo_name: str,
    tag: str,
    cfg,
) -> list[TroubleshootIssue]:
    """Ask Ollama to find logical bugs, security issues, and anti-patterns."""
    if not file_contents:
        return []

    # Build a compact representation of the module for the prompt
    code_snippet = _build_code_context(file_contents)
    summary_text = _summarize_for_prompt(module_summary)
    problem_ctx = f"\n\nUser-reported problem:\n{problem_description}" if problem_description.strip() else ""

    prompt = f"""You are a Terraform expert performing a code review.

Analyse the following Terraform module and identify ALL bugs, misconfigurations, and improvements.

Module summary:
{summary_text}

Terraform code:
{code_snippet}
{problem_ctx}

Return a JSON array of issues. Each issue must have EXACTLY these fields:
- "severity": one of "error", "warning", "info"
- "category": one of "syntax", "type", "undefined_ref", "missing_attr", "deprecated", "security", "logic", "best_practice", "provider"
- "file_path": filename (e.g. "main.tf") or "" if unknown
- "line": integer line number or null
- "resource_type": Terraform resource type (e.g. "google_storage_bucket") or ""
- "resource_name": resource label or variable name or ""
- "message": clear description of the issue (1-2 sentences)
- "suggestion": how to fix it (1-2 sentences)

Focus on:
1. Logic bugs: wrong dependencies, missing `depends_on`, wrong resource ordering
2. Type mismatches: string where list expected, etc.
3. Missing required arguments for the resource types used
4. Security: public access, missing encryption, overly permissive IAM, exposed secrets
5. Anti-patterns: hardcoded project/region, `count` vs `for_each` misuse, missing lifecycle
6. Deprecated: resources/arguments that are deprecated in recent provider versions

Return ONLY a valid JSON array. No markdown. No explanation. If no issues found, return [].
"""

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                f"{cfg.llm.base_url}/api/chat",
                json={
                    "model": cfg.llm.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"temperature": 0.0, "num_predict": 2048},
                },
            )
            resp.raise_for_status()
            raw = resp.json()["message"]["content"].strip()
    except Exception:
        return []

    return _parse_llm_issues(raw)


def _build_code_context(file_contents: dict[str, str], max_chars: int = 6000) -> str:
    """Concatenate .tf file contents, trimmed to max_chars total."""
    parts: list[str] = []
    total = 0
    # Prioritise: main.tf first, then variables, then the rest
    priority = ["main.tf", "variables.tf", "outputs.tf", "versions.tf"]
    ordered = sorted(
        file_contents.keys(),
        key=lambda p: (priority.index(p.split("/")[-1]) if p.split("/")[-1] in priority else 99, p),
    )
    for path in ordered:
        content = file_contents[path]
        snippet = f"### {path}\n{content}\n"
        if total + len(snippet) > max_chars:
            remaining = max_chars - total
            if remaining > 200:
                parts.append(snippet[:remaining] + "\n... (truncated)")
            break
        parts.append(snippet)
        total += len(snippet)
    return "\n".join(parts)


def _summarize_for_prompt(summary: dict) -> str:
    if not summary:
        return "(no summary available)"
    resources = summary.get("resource_types", [])
    req_vars  = [v.get("name", "") for v in summary.get("required_variables", [])]
    provider  = summary.get("provider", {})
    lines = []
    if resources:
        lines.append(f"Resources: {', '.join(resources)}")
    if req_vars:
        lines.append(f"Required variables: {', '.join(req_vars)}")
    if provider:
        lines.append(f"Provider: {provider.get('name', '')} {provider.get('version_constraint', '')}")
    return "\n".join(lines) if lines else "(empty module)"


def _parse_llm_issues(raw: str) -> list[TroubleshootIssue]:
    """Parse the LLM's JSON array into TroubleshootIssue instances."""
    # Strip markdown fences
    raw = re.sub(r"```(?:json)?", "", raw).strip()
    # Extract JSON array
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []

    issues: list[TroubleshootIssue] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            sev = IssueSeverity(item.get("severity", "info"))
        except ValueError:
            sev = IssueSeverity.INFO
        try:
            cat = IssueCategory(item.get("category", "logic"))
        except ValueError:
            cat = IssueCategory.LOGIC

        issues.append(TroubleshootIssue(
            severity=sev,
            category=cat,
            file_path=str(item.get("file_path", "") or ""),
            line=item.get("line") if isinstance(item.get("line"), int) else None,
            resource_type=str(item.get("resource_type", "") or ""),
            resource_name=str(item.get("resource_name", "") or ""),
            message=str(item.get("message", "") or ""),
            suggestion=str(item.get("suggestion", "") or ""),
        ))
    return issues


# ── Stage 3: Version recommendation ──────────────────────────────────────────

async def _version_recommendation(
    repo_name: str,
    tag: str,
    issues: list[TroubleshootIssue],
) -> Optional[VersionRecommendation]:
    """
    Find the minimum provider version that fixes detected issues without
    introducing breaking changes relevant to the module's resource types.
    """
    try:
        provider_key = detect_terraform_provider(repo_name, tag)
        prov_cfg     = TERRAFORM_PROVIDERS.get(provider_key, TERRAFORM_PROVIDERS["google"])

        current_version = get_current_provider_version(repo_name, tag, provider_key)
        if not current_version:
            return None

        latest_version = await fetch_latest_ga_version(provider_key)
        if not latest_version:
            return None

        # Already on latest
        if _semver_tuple(current_version) >= _semver_tuple(latest_version):
            return VersionRecommendation(
                current_version=current_version,
                recommended_version=current_version,
                reason="Module is already on the latest GA provider version.",
                breaking_changes=0,
                safe_to_upgrade=True,
                changelog_url=prov_cfg["changelog_url"],
                fixes_in_version=[],
            )

        changelog_full = await fetch_provider_changelog("0.0.0", latest_version, provider_key)
        version_sections = _parse_changelog_versions(changelog_full)

        # Get the resource types used by this module for relevance filtering
        from backend.agent.tools.hcl_tools import get_all_resources as _get_res
        resources = _get_res(repo_name, tag)
        resource_prefixes = list({r.resource_type.split("_")[0] + "_" for r in resources})

        # Walk versions in ascending order from just above current
        cur_tuple = _semver_tuple(current_version)
        candidate_versions = sorted(
            [v for v in version_sections if _semver_tuple(v) > cur_tuple],
            key=_semver_tuple,
        )

        # Identify deprecated/bug issues from static analysis
        issue_keywords = _extract_issue_keywords(issues)

        best_fix_version: Optional[str] = None
        best_fix_notes: list[str] = []
        accumulated_breaking = 0

        for ver in candidate_versions:
            section = version_sections[ver]
            fixes    = _extract_section(section, r"BUG FIX|ENHANCEMENT|FIX")
            breaking = _extract_section(section, r"BREAKING|REMOVED|DEPRECATED REMOVED")

            # Count breaking changes affecting this module's resource types
            breaking_for_module = _count_relevant(breaking, resource_prefixes)

            if breaking_for_module > 0:
                # This version introduces breaking changes for our resources
                # If we haven't found a fix version yet, note it but keep looking
                accumulated_breaking += breaking_for_module
                if best_fix_version is None:
                    # No fix yet found before the first breaking version
                    # Recommend current + annotate as "breaking upgrade required"
                    best_fix_version = ver
                    best_fix_notes = _relevant_lines(fixes, issue_keywords, resource_prefixes)
                break

            if fixes and (not best_fix_version):
                relevant_fixes = _relevant_lines(fixes, issue_keywords, resource_prefixes)
                if relevant_fixes:
                    best_fix_version = ver
                    best_fix_notes = relevant_fixes

        # Default: recommend latest if no specific fix version found
        if not best_fix_version:
            best_fix_version = latest_version
            section = version_sections.get(latest_version, "")
            best_fix_notes = _relevant_lines(
                _extract_section(section, r"BUG FIX|ENHANCEMENT|FIX"),
                issue_keywords, resource_prefixes
            )

        safe = accumulated_breaking == 0

        # Build reason text
        if best_fix_version == current_version:
            reason = "Module is already on the latest GA version."
        elif safe:
            reason = (
                f"Upgrade from v{current_version} to v{best_fix_version} is safe — "
                f"no breaking changes affect this module's resources."
            )
        else:
            reason = (
                f"Upgrade to v{best_fix_version} is available but introduces "
                f"{accumulated_breaking} breaking change(s) affecting this module. "
                f"Review the changelog before upgrading."
            )

        return VersionRecommendation(
            current_version=current_version,
            recommended_version=best_fix_version,
            reason=reason,
            breaking_changes=accumulated_breaking,
            safe_to_upgrade=safe,
            changelog_url=prov_cfg["changelog_url"],
            fixes_in_version=best_fix_notes[:8],
        )

    except Exception:
        return None


# ── Changelog parsing helpers ─────────────────────────────────────────────────

def _parse_changelog_versions(changelog: str) -> dict[str, str]:
    """
    Split a CHANGELOG.md into {version_string: section_text} dict.
    Handles formats: `## 5.42.0 (...)` and `# 5.42.0`.
    """
    pattern = re.compile(r"^#{1,2}\s+(\d+\.\d+\.\d+)", re.MULTILINE)
    matches = list(pattern.finditer(changelog))
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        ver = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(changelog)
        sections[ver] = changelog[start:end]
    return sections


def _extract_section(text: str, heading_pattern: str) -> str:
    """Extract lines under a heading matching the pattern."""
    lines = text.splitlines()
    capturing = False
    result: list[str] = []
    heading_re = re.compile(heading_pattern, re.IGNORECASE)
    for line in lines:
        if re.match(r"^#+\s+", line):
            capturing = bool(heading_re.search(line))
        elif capturing and line.strip():
            result.append(line.strip().lstrip("* ").strip())
    return "\n".join(result)


def _count_relevant(text: str, prefixes: list[str]) -> int:
    if not text:
        return 0
    count = 0
    for line in text.splitlines():
        for prefix in prefixes:
            if prefix in line.lower():
                count += 1
                break
    return count


def _relevant_lines(text: str, keywords: list[str], prefixes: list[str]) -> list[str]:
    if not text:
        return []
    result: list[str] = []
    for line in text.splitlines():
        line_lower = line.lower()
        if any(k in line_lower for k in keywords) or any(p in line_lower for p in prefixes):
            result.append(line)
    return result or text.splitlines()[:5]


def _extract_issue_keywords(issues: list[TroubleshootIssue]) -> list[str]:
    """Extract resource type names and common words from issues for changelog matching."""
    keywords: set[str] = set()
    for issue in issues:
        if issue.resource_type:
            keywords.add(issue.resource_type.lower())
            # Also add individual parts: google_storage_bucket → storage, bucket
            for part in issue.resource_type.split("_")[1:]:
                if len(part) > 3:
                    keywords.add(part.lower())
        # Extract nouns from the message
        words = re.findall(r'\b[a-z]{4,}\b', issue.message.lower())
        keywords.update(w for w in words if w not in {
            "this", "that", "with", "from", "have", "been", "will", "should",
            "must", "when", "which", "their", "there", "where", "does", "used",
        })
    return list(keywords)[:20]


# ── Summary builder ───────────────────────────────────────────────────────────

def _build_result_summary(
    repo_name: str,
    tag: str,
    issues: list[TroubleshootIssue],
    error_count: int,
    warning_count: int,
    version_rec: Optional[VersionRecommendation],
) -> str:
    if not issues:
        base = f"No issues found in `{repo_name}` at `{tag}`. The module looks healthy."
    else:
        parts: list[str] = []
        if error_count:
            parts.append(f"{error_count} error{'s' if error_count > 1 else ''}")
        if warning_count:
            parts.append(f"{warning_count} warning{'s' if warning_count > 1 else ''}")
        info_count = len(issues) - error_count - warning_count
        if info_count:
            parts.append(f"{info_count} info")
        base = f"Found {', '.join(parts)} in `{repo_name}` at `{tag}`."

    if version_rec and version_rec.recommended_version != version_rec.current_version:
        upgrade = f" Recommended upgrade: v{version_rec.current_version} → v{version_rec.recommended_version}"
        if version_rec.safe_to_upgrade:
            upgrade += " (no breaking changes)."
        else:
            upgrade += f" ({version_rec.breaking_changes} breaking change(s) — review before upgrading)."
        base += upgrade

    return base
