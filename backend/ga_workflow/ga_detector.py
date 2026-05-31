"""
ga_detector.py — Detects the latest GA release for any Terraform provider
(Google/GCP, AWS, Azure) and maps changelog changes to the module.

Multi-cloud support (v2.3):
  - Auto-detects provider from versions.tf (google / aws / azurerm)
  - Fetches latest GA version from Terraform Registry for any provider
  - Downloads provider changelog from the correct GitHub repo
  - Enhanced breaking-change detection with BreakingReason classification
  - LLM analysis understands all three providers

Sources per provider:
  Google: registry.terraform.io/hashicorp/google + github.com/hashicorp/terraform-provider-google
  AWS:    registry.terraform.io/hashicorp/aws    + github.com/hashicorp/terraform-provider-aws
  Azure:  registry.terraform.io/hashicorp/azurerm+ github.com/hashicorp/terraform-provider-azurerm
"""
from __future__ import annotations

import re
from typing import Optional
import httpx

from backend.config import get_config, RepoConfig
from backend.agent.tools.git_tools import get_latest_tag, get_file_at_tag, list_tf_files_at_tag
from backend.agent.tools.hcl_tools import get_all_resources, get_provider_requirements
from backend.ga_workflow.ga_models import (
    GAChange, GARelease, GAChangeSet, ChangeType, BreakingReason, CloudProvider,
    WorkflowRun, WorkflowStage,
)


HTTP_TIMEOUT = 15.0

# ── Per-provider configuration ────────────────────────────────────────────────

TERRAFORM_PROVIDERS: dict[str, dict] = {
    "google": {
        "registry_id":     "hashicorp/google",
        "github_repo":     "hashicorp/terraform-provider-google",
        "resource_prefix": "google_",
        "cloud_provider":  CloudProvider.GCP,
        "display_name":    "Google Cloud (GCP)",
        "changelog_url":   "https://raw.githubusercontent.com/hashicorp/terraform-provider-google/main/CHANGELOG.md",
        "releases_url":    "https://api.github.com/repos/hashicorp/terraform-provider-google/releases",
    },
    "aws": {
        "registry_id":     "hashicorp/aws",
        "github_repo":     "hashicorp/terraform-provider-aws",
        "resource_prefix": "aws_",
        "cloud_provider":  CloudProvider.AWS,
        "display_name":    "Amazon Web Services (AWS)",
        "changelog_url":   "https://raw.githubusercontent.com/hashicorp/terraform-provider-aws/main/CHANGELOG.md",
        "releases_url":    "https://api.github.com/repos/hashicorp/terraform-provider-aws/releases",
    },
    "azurerm": {
        "registry_id":     "hashicorp/azurerm",
        "github_repo":     "hashicorp/terraform-provider-azurerm",
        "resource_prefix": "azurerm_",
        "cloud_provider":  CloudProvider.AZURE,
        "display_name":    "Microsoft Azure",
        "changelog_url":   "https://raw.githubusercontent.com/hashicorp/terraform-provider-azurerm/main/CHANGELOG.md",
        "releases_url":    "https://api.github.com/repos/hashicorp/terraform-provider-azurerm/releases",
    },
}


# ── Provider auto-detection ───────────────────────────────────────────────────

def detect_terraform_provider(repo_name: str, tag: str) -> str:
    """
    Read versions.tf at the given tag and return the provider key
    ('google', 'aws', 'azurerm'). Falls back to 'google' if detection fails.
    """
    try:
        reqs = get_provider_requirements(repo_name, tag)
        if not reqs:
            return "google"
        providers = reqs.get("required_providers", {})
        for key in ("google", "aws", "azurerm"):
            if key in providers:
                return key
        # Scan for source fields like "hashicorp/aws"
        for _key, val in providers.items():
            if isinstance(val, list):
                val = val[0] if val else {}
            src = str(val.get("source", ""))
            for key in ("google", "aws", "azurerm"):
                if key in src:
                    return key
    except Exception:
        pass
    return "google"


# ── Version fetching ──────────────────────────────────────────────────────────

async def fetch_latest_ga_version(provider_key: str = "google") -> Optional[str]:
    """Fetch latest stable (non-beta/alpha/rc) provider version from Terraform Registry."""
    cfg = TERRAFORM_PROVIDERS.get(provider_key, TERRAFORM_PROVIDERS["google"])
    url = f"https://registry.terraform.io/v1/providers/{cfg['registry_id']}/versions"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(url)
            r.raise_for_status()
            versions = [
                v["version"] for v in r.json().get("versions", [])
                if re.match(r"^\d+\.\d+\.\d+$", v.get("version", ""))
            ]
            if not versions:
                return None
            versions.sort(key=_semver_tuple, reverse=True)
            return versions[0]
    except Exception as e:
        print(f"[ga_detector] Registry API error ({provider_key}): {e}")
        return None


async def fetch_provider_changelog(
    from_version: str,
    to_version: str,
    provider_key: str = "google",
) -> str:
    """Fetch CHANGELOG.md for the given provider and extract the version range."""
    cfg = TERRAFORM_PROVIDERS.get(provider_key, TERRAFORM_PROVIDERS["google"])
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            r = await client.get(cfg["changelog_url"])
            r.raise_for_status()
            return _extract_changelog_range(r.text, from_version, to_version)
    except Exception as e:
        print(f"[ga_detector] Changelog fetch error ({provider_key}): {e}")
        return f"Could not fetch changelog: {e}"


async def fetch_github_release_notes(
    version: str,
    provider_key: str = "google",
    github_token: Optional[str] = None,
) -> str:
    """Fetch release notes for a specific provider version from GitHub Releases API."""
    cfg = TERRAFORM_PROVIDERS.get(provider_key, TERRAFORM_PROVIDERS["google"])
    headers = {}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers) as client:
            for tag in (f"v{version}", version):
                r = await client.get(f"{cfg['releases_url']}/tags/{tag}")
                if r.status_code == 200:
                    return r.json().get("body", "")
    except Exception as e:
        print(f"[ga_detector] GitHub releases error ({provider_key}): {e}")
    return ""


# ── Current version extraction ────────────────────────────────────────────────

def get_current_provider_version(
    repo_name: str,
    tag: str,
    provider_key: Optional[str] = None,
) -> Optional[str]:
    """
    Read the current provider version constraint from versions.tf.
    If provider_key is None, auto-detects the provider first.
    """
    reqs = get_provider_requirements(repo_name, tag)
    if not reqs:
        return None

    providers = reqs.get("required_providers", {})
    key = provider_key or detect_terraform_provider(repo_name, tag)

    # Try the exact provider key, then fallback aliases
    candidates = [key]
    if key == "google":
        candidates.append("google-beta")

    for candidate in candidates:
        prov = providers.get(candidate)
        if prov:
            if isinstance(prov, list):
                prov = prov[0] if prov else {}
            version_str = (
                prov.get("version")
                or prov.get("version_constraint")
                or "unknown"
            )
            match = re.search(r"(\d+\.\d+\.\d+)", str(version_str))
            return match.group(1) if match else str(version_str)

    return None


# ── Changelog parsing ─────────────────────────────────────────────────────────

# Breaking-change pattern map: (regex, BreakingReason)
_BREAKING_PATTERNS: list[tuple[str, BreakingReason]] = [
    (r"\bremoved?\b",                   BreakingReason.REMOVED),
    (r"\bno longer (?:supported|valid|exist)\b", BreakingReason.REMOVED),
    (r"\brenamed?\s+to\b",              BreakingReason.RENAMED),
    (r"\btype changed\b",               BreakingReason.TYPE_CHANGED),
    (r"\btype has changed\b",           BreakingReason.TYPE_CHANGED),
    (r"\bstring\s+→\s+(?:list|map|set|number|bool)\b", BreakingReason.TYPE_CHANGED),
    (r"\bnow required\b",               BreakingReason.REQUIRED_NOW),
    (r"\bis now required\b",            BreakingReason.REQUIRED_NOW),
    (r"\bno longer optional\b",         BreakingReason.REQUIRED_NOW),
    (r"\bdefault.*changed\b",           BreakingReason.BEHAVIOR_CHANGED),
    (r"\bbreaking\b",                   BreakingReason.UNKNOWN),
    (r"\bdeprecated and removed\b",     BreakingReason.DEPRECATED_REMOVED),
    (r"\bpreviously deprecated.*removed\b", BreakingReason.DEPRECATED_REMOVED),
    (r"\bmust now\b",                   BreakingReason.BEHAVIOR_CHANGED),
]

_BREAKING_KEYWORDS = [
    "breaking", "removed", "no longer", "deprecated and removed",
    "must now", "required", "renamed to", "type changed",
    "is now required", "no longer optional",
]


def classify_breaking(line: str) -> tuple[bool, Optional[BreakingReason]]:
    """Return (is_breaking, reason) for a changelog line."""
    lower = line.lower()
    for pattern, reason in _BREAKING_PATTERNS:
        if re.search(pattern, lower):
            return True, reason
    return False, None


def parse_changelog_to_changes(
    changelog_text: str,
    resource_types_in_module: list[str],
    target_version: str,
    provider_key: str = "google",
) -> list[GAChange]:
    """
    Parse changelog text into structured GAChange objects.
    Works for Google, AWS, and Azure changelogs (common Markdown format).
    """
    cloud_provider = TERRAFORM_PROVIDERS.get(provider_key, TERRAFORM_PROVIDERS["google"])["cloud_provider"]
    resource_prefix = TERRAFORM_PROVIDERS.get(provider_key, TERRAFORM_PROVIDERS["google"])["resource_prefix"]

    # Build patterns using the provider's resource prefix
    pfx = re.escape(resource_prefix)

    PATTERNS = [
        # New argument on existing resource
        (rf"resource[/`]?({pfx}[\w_]+)[`]?[:\s]+added.*?(?:argument|field|attribute)[s]?[`\s]+[`]?([\w_]+)[`]?",
         ChangeType.NEW_ARGUMENT),
        # Deprecated argument
        (rf"resource[/`]?({pfx}[\w_]+)[`]?[:\s]+[`]?([\w_]+)[`]?\s+is\s+deprecated",
         ChangeType.DEPRECATED_ARG),
        # New resource
        (rf"\*\*New Resource[:\*\*]+\s*[`]?({pfx}[\w_]+)[`]?",
         ChangeType.NEW_RESOURCE),
        # IAM change
        (rf"resource[/`]?({pfx}[\w_]+)[`]?[:\s]+.*?iam.*?(?:added|support|binding)",
         ChangeType.IAM_CHANGE),
    ]

    changes: list[GAChange] = []
    changelog_url = TERRAFORM_PROVIDERS.get(provider_key, TERRAFORM_PROVIDERS["google"])["changelog_url"]

    for line in changelog_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        breaking, reason = classify_breaking(line)

        for pattern, change_type in PATTERNS:
            m = re.search(pattern, line, re.IGNORECASE)
            if not m:
                continue

            groups = m.groups()
            resource_type = groups[0] if groups else "unknown"
            attribute = groups[1] if len(groups) > 1 else None

            if (change_type != ChangeType.NEW_RESOURCE
                    and resource_types_in_module
                    and resource_type not in resource_types_in_module):
                continue

            migration_note = _migration_note(change_type, resource_type, attribute, reason) if breaking else None

            changes.append(GAChange(
                change_type=change_type,
                resource_type=resource_type,
                attribute_name=attribute,
                description=line[:300],
                provider_version=target_version,
                breaking=breaking,
                breaking_reason=reason,
                migration_note=migration_note,
                cloud_provider=cloud_provider,
                source_url=changelog_url,
            ))
            break

    return changes


def _migration_note(
    change_type: ChangeType,
    resource: str,
    attribute: Optional[str],
    reason: Optional[BreakingReason],
) -> str:
    if reason == BreakingReason.REMOVED:
        what = f"`{resource}.{attribute}`" if attribute else f"resource `{resource}`"
        return f"{what} has been removed. Delete or replace all usages before upgrading."
    if reason == BreakingReason.RENAMED:
        what = f"`{attribute}`" if attribute else f"`{resource}`"
        return f"{what} was renamed. Update the identifier in all .tf files."
    if reason == BreakingReason.TYPE_CHANGED:
        what = f"`{attribute}`" if attribute else f"`{resource}`"
        return f"Type of {what} changed. Review and update variable assignments."
    if reason == BreakingReason.REQUIRED_NOW:
        what = f"`{attribute}`" if attribute else f"a field in `{resource}`"
        return f"{what} is now required. Supply a value in all module calls."
    if reason == BreakingReason.BEHAVIOR_CHANGED:
        return "Behaviour changed — verify current default values still match your intent."
    if reason == BreakingReason.DEPRECATED_REMOVED:
        what = f"`{attribute}`" if attribute else f"`{resource}`"
        return f"{what} was deprecated and is now removed. Migrate before upgrading."
    return "Review the changelog entry and update affected .tf files accordingly."


# ── LLM-powered change analysis ───────────────────────────────────────────────

async def analyze_changes_with_llm(
    changelog_text: str,
    module_summary: dict,
    run: WorkflowRun,
    provider_key: str = "google",
) -> list[GAChange]:
    """
    Use the local LLM to map changelog changes to module resources.
    Falls back to empty list if LLM is unavailable.
    """
    cfg = get_config()
    from openai import AsyncOpenAI
    client = AsyncOpenAI(
        base_url=cfg.llm.base_url.rstrip("/") + "/v1",
        api_key="ollama",
    )

    prov_cfg = TERRAFORM_PROVIDERS.get(provider_key, TERRAFORM_PROVIDERS["google"])
    cloud_name = prov_cfg["display_name"]
    resource_prefix = prov_cfg["resource_prefix"]

    prompt = f"""You are a Terraform module expert analysing a {cloud_name} provider changelog.

MODULE SUMMARY:
{module_summary}

CHANGELOG EXCERPT:
{changelog_text[:4000]}

Return a JSON array. Each object must have exactly these fields:
- change_type: one of: new_resource, new_argument, deprecated_argument, new_variable, updated_variable, provider_version, new_output, iam_change, api_requirement, lifecycle_change
- resource_type: the {resource_prefix}xxx resource type
- attribute_name: the specific field/argument name, or null
- description: one sentence describing the change
- provider_version: the provider version string
- breaking: true or false
- breaking_reason: one of: removed, renamed, type_changed, required_now, behavior_changed, deprecated_removed, unknown — or null if not breaking
- migration_note: plain-English migration guidance, or null
- migration_guide: HCL snippet showing how to migrate, or null

Return ONLY the JSON array, no markdown, no explanation."""

    try:
        resp = await client.chat.completions.create(
            model=cfg.llm.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=2000,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("```").strip()

        import json
        cloud_provider = prov_cfg["cloud_provider"]
        changes = []
        for item in json.loads(raw):
            try:
                # Inject cloud_provider since the LLM won't set it
                item.setdefault("cloud_provider", cloud_provider.value)
                changes.append(GAChange(**item))
            except Exception:
                pass
        run.log(f"LLM identified {len(changes)} relevant changes ({cloud_name})")
        return changes
    except Exception as e:
        run.log(f"LLM analysis failed ({e}), falling back to regex parser", level="warning")
        return []


# ── Main entry point ──────────────────────────────────────────────────────────

async def detect_ga_release(
    repo_name: str,
    run: WorkflowRun,
    github_token: Optional[str] = None,
    provider_key: Optional[str] = None,
) -> Optional[GAChangeSet]:
    """
    Full GA detection pipeline for any supported Terraform provider:
    1. Auto-detect provider from versions.tf (or use override)
    2. Get current provider version
    3. Fetch latest GA version from Terraform Registry
    4. Download and parse changelog between the two versions
    5. LLM maps changes to module resources with breaking-reason classification
    6. Returns a structured GAChangeSet
    """
    cfg = get_config()
    repo_cfg = cfg.get_repo(repo_name)
    if not repo_cfg:
        run.fail(f"Repo '{repo_name}' not found in config")
        return None

    run.stage = WorkflowStage.DETECTING
    run.log(f"Detecting GA release for {repo_name}")

    current_tag = get_latest_tag(repo_name) or "main"
    run.log(f"Analysing current tag: {current_tag}")

    # Auto-detect provider if not specified
    key = provider_key or detect_terraform_provider(repo_name, current_tag)
    prov_cfg = TERRAFORM_PROVIDERS.get(key, TERRAFORM_PROVIDERS["google"])
    run.log(f"Detected provider: {prov_cfg['display_name']} ({key})")

    current_version = get_current_provider_version(repo_name, current_tag, key)
    if not current_version:
        run.log("Could not determine current provider version — defaulting to '4.0.0'", level="warning")
        current_version = "4.0.0"

    run.log(f"Current provider version: {current_version}")

    latest_version = await fetch_latest_ga_version(key)
    if not latest_version:
        run.log("Could not fetch latest GA version from Terraform Registry", level="warning")
        latest_version = current_version

    run.log(f"Latest GA version: {latest_version}")

    upgrade_required = _semver_tuple(latest_version) > _semver_tuple(current_version)

    ga_release = GARelease(
        provider=prov_cfg["registry_id"],
        cloud_provider=prov_cfg["cloud_provider"],
        current_version=current_version,
        latest_ga_version=latest_version,
        upgrade_required=upgrade_required,
        changelog_url=prov_cfg["changelog_url"],
    )

    if not upgrade_required:
        run.log("Module is already on the latest GA provider version — no changes needed")
        return GAChangeSet(
            repo_name=repo_name,
            gcp_product=repo_cfg.gcp_product,
            current_tag=current_tag,
            ga_release=ga_release,
            changes=[],
            files_to_modify=[],
            summary=f"Module is already on the latest GA {prov_cfg['display_name']} provider version. No changes required.",
        )

    run.log(f"Fetching changelog from v{current_version} to v{latest_version}")
    changelog_text = await fetch_provider_changelog(current_version, latest_version, key)

    release_notes = await fetch_github_release_notes(latest_version, key, github_token)
    if release_notes:
        changelog_text = f"{release_notes}\n\n{changelog_text}"

    resources = get_all_resources(repo_name, current_tag)
    resource_types = list({r.resource_type for r in resources})
    run.log(f"Module uses {len(resource_types)} resource types")

    from backend.agent.tools.hcl_tools import summarize_module
    module_summary = summarize_module(repo_name, current_tag)

    run.stage = WorkflowStage.ANALYZING
    llm_changes = await analyze_changes_with_llm(changelog_text, module_summary, run, key)

    if not llm_changes:
        changes = parse_changelog_to_changes(changelog_text, resource_types, latest_version, key)
        run.log(f"Regex parser found {len(changes)} relevant changes")
    else:
        changes = llm_changes

    breaking = sum(1 for c in changes if c.breaking)
    ga_release.breaking_changes = breaking
    ga_release.new_features = len(changes) - breaking

    tf_files = list_tf_files_at_tag(repo_name, current_tag)
    files_to_modify = _map_changes_to_files(changes, tf_files)
    summary = _build_summary(ga_release, changes, repo_cfg.gcp_product, prov_cfg["display_name"])

    return GAChangeSet(
        repo_name=repo_name,
        gcp_product=repo_cfg.gcp_product,
        current_tag=current_tag,
        ga_release=ga_release,
        changes=changes,
        files_to_modify=files_to_modify,
        summary=summary,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _semver_tuple(version: str) -> tuple:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", str(version))
    if m:
        return tuple(int(x) for x in m.groups())
    return (0, 0, 0)


def _extract_changelog_range(full_text: str, from_ver: str, to_ver: str) -> str:
    lines = full_text.splitlines()
    capturing = False
    result_lines = []
    from_tuple = _semver_tuple(from_ver)
    to_tuple = _semver_tuple(to_ver)

    for line in lines:
        ver_match = re.search(r"##\s+v?(\d+\.\d+\.\d+)", line)
        if ver_match:
            ver = _semver_tuple(ver_match.group(1))
            if from_tuple < ver <= to_tuple:
                capturing = True
            elif ver <= from_tuple:
                capturing = False
        if capturing:
            result_lines.append(line)

    result = "\n".join(result_lines)
    return result[:8000] if result else full_text[:4000]


def _map_changes_to_files(changes: list[GAChange], tf_files: list[str]) -> list[str]:
    affected = set()
    for change in changes:
        for f in tf_files:
            fname = f.lower()
            if "main.tf" in fname:
                affected.add(f)
            if change.change_type == ChangeType.PROVIDER_VERSION and "versions.tf" in fname:
                affected.add(f)
            if change.change_type in (ChangeType.NEW_VARIABLE, ChangeType.UPDATED_VARIABLE,
                                      ChangeType.REMOVED_VARIABLE) and "variables.tf" in fname:
                affected.add(f)
            if change.change_type == ChangeType.NEW_OUTPUT and "outputs.tf" in fname:
                affected.add(f)
            if change.change_type == ChangeType.IAM_CHANGE and "iam" in fname:
                affected.add(f)
    return sorted(affected)


def _build_summary(
    release: GARelease,
    changes: list[GAChange],
    product: str,
    display_name: str,
) -> str:
    if not changes:
        return (
            f"No changes detected for the {product} module between "
            f"{display_name} provider v{release.current_version} and v{release.latest_ga_version}."
        )
    breaking = [c for c in changes if c.breaking]
    features = [c for c in changes if not c.breaking]
    parts = [
        f"{display_name} provider upgrade from v{release.current_version} to v{release.latest_ga_version} "
        f"for the {product} module introduces {len(changes)} changes: "
        f"{len(features)} new features and {len(breaking)} breaking changes."
    ]
    if breaking:
        reasons = set(c.breaking_reason.value for c in breaking if c.breaking_reason)
        parts.append(f"Breaking reasons: {', '.join(sorted(reasons))}.")
        parts.append(f"First breaking change: {breaking[0].description[:100]}.")
    return " ".join(parts)
