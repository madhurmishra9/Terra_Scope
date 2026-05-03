"""
ga_detector.py — Detects the latest GA release of the Google Terraform provider
and maps changes to the module's current codebase.

Sources:
  1. Terraform Registry API  → latest provider version
  2. GitHub Releases API     → provider changelog
  3. Provider schema API     → resource attribute diffs (via Ollama-powered analysis)

All network calls have timeouts and fallback behavior.
"""
from __future__ import annotations

import re
from typing import Optional
import httpx

from backend.config import get_config, RepoConfig
from backend.agent.tools.git_tools import get_latest_tag, get_file_at_tag, list_tf_files_at_tag
from backend.agent.tools.hcl_tools import get_all_resources, get_provider_requirements
from backend.ga_workflow.ga_models import (
    GAChange, GARelease, GAChangeSet, ChangeType, WorkflowRun, WorkflowStage
)


REGISTRY_API     = "https://registry.terraform.io/v1/providers/hashicorp/google"
GITHUB_RELEASES  = "https://api.github.com/repos/hashicorp/terraform-provider-google/releases"
GITHUB_CHANGELOG = "https://raw.githubusercontent.com/hashicorp/terraform-provider-google/main/CHANGELOG.md"

HTTP_TIMEOUT = 15.0


# ── Provider version detection ─────────────────────────────────────────────────

async def fetch_latest_ga_version() -> Optional[str]:
    """
    Fetch the latest stable (non-beta, non-alpha) Google provider version
    from the Terraform Registry API.
    """
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(f"{REGISTRY_API}/versions")
            r.raise_for_status()
            data = r.json()
            versions = data.get("versions", [])
            # Filter to GA only: no alpha, beta, rc suffixes
            ga_versions = [
                v["version"] for v in versions
                if re.match(r"^\d+\.\d+\.\d+$", v.get("version", ""))
            ]
            if not ga_versions:
                return None
            # Sort semantically and return highest
            ga_versions.sort(key=_semver_tuple, reverse=True)
            return ga_versions[0]
    except Exception as e:
        print(f"[ga_detector] Registry API error: {e}")
        return None


async def fetch_provider_changelog(from_version: str, to_version: str) -> str:
    """
    Fetch the raw CHANGELOG.md from the provider GitHub repo and extract
    the section relevant to the version range [from_version → to_version].
    """
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            r = await client.get(GITHUB_CHANGELOG)
            r.raise_for_status()
            full_changelog = r.text
            return _extract_changelog_range(full_changelog, from_version, to_version)
    except Exception as e:
        print(f"[ga_detector] Changelog fetch error: {e}")
        return f"Could not fetch changelog: {e}"


async def fetch_github_release_notes(version: str, github_token: Optional[str] = None) -> str:
    """Fetch release notes for a specific version from GitHub Releases API."""
    headers = {}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers) as client:
            r = await client.get(f"{GITHUB_RELEASES}/tags/v{version}")
            if r.status_code == 200:
                return r.json().get("body", "")
            # Try without 'v' prefix
            r = await client.get(f"{GITHUB_RELEASES}/tags/{version}")
            if r.status_code == 200:
                return r.json().get("body", "")
    except Exception as e:
        print(f"[ga_detector] GitHub releases error: {e}")
    return ""


# ── Current version extraction ─────────────────────────────────────────────────

def get_current_provider_version(repo_name: str, tag: str) -> Optional[str]:
    """
    Read the current google provider version constraint from versions.tf
    at the given tag. Returns the version string or None.
    """
    reqs = get_provider_requirements(repo_name, tag)
    if not reqs:
        return None

    providers = reqs.get("required_providers", {})

    # Handle different HCL parse structures
    google_provider = providers.get("google") or providers.get("google-beta")
    if not google_provider:
        return None

    if isinstance(google_provider, list):
        google_provider = google_provider[0] if google_provider else {}

    version_str = (
        google_provider.get("version")
        or google_provider.get("version_constraint")
        or "unknown"
    )

    # Extract the first concrete version number from constraint like ">= 4.0, < 6.0"
    match = re.search(r"(\d+\.\d+\.\d+)", str(version_str))
    return match.group(1) if match else str(version_str)


# ── Changelog parsing ─────────────────────────────────────────────────────────

def parse_changelog_to_changes(
    changelog_text: str,
    resource_types_in_module: list[str],
    target_version: str,
) -> list[GAChange]:
    """
    Parse changelog text into structured GAChange objects.
    Only returns changes relevant to resource types used in the module.
    """
    changes: list[GAChange] = []

    # Patterns for common changelog entry formats
    PATTERNS = [
        # New resource: "resource/google_bigquery_dataset: added new argument `max_time_travel_hours`"
        (r"resource[/`]?(google[\w_]+)[`]?[:\s]+added.*?(?:argument|field|attribute)[s]?[`\s]+[`]?([\w_]+)[`]?",
         ChangeType.NEW_ARGUMENT),
        # Deprecated: "resource/google_bigquery_dataset: `foo` is deprecated"
        (r"resource[/`]?(google[\w_]+)[`]?[:\s]+[`]?([\w_]+)[`]?\s+is\s+deprecated",
         ChangeType.DEPRECATED_ARG),
        # New resource type: "**New Resource:** `google_bigquery_connection`"
        (r"\*\*New Resource[:\*\*]+\s*[`]?(google[\w_]+)[`]?",
         ChangeType.NEW_RESOURCE),
        # Provider version requirement
        (r"provider.*?version.*?(\d+\.\d+\.\d+)",
         ChangeType.PROVIDER_VERSION),
        # IAM changes
        (r"resource[/`]?(google[\w_]+)[`]?[:\s]+.*?iam.*?(?:added|support|binding)",
         ChangeType.IAM_CHANGE),
    ]

    lines = changelog_text.splitlines()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        for pattern, change_type in PATTERNS:
            m = re.search(pattern, line, re.IGNORECASE)
            if not m:
                continue

            groups = m.groups()
            resource_type = groups[0] if groups else "unknown"
            attribute = groups[1] if len(groups) > 1 else None

            # Skip if this resource isn't used in the module (unless it's a new resource)
            if (change_type != ChangeType.NEW_RESOURCE
                    and resource_types_in_module
                    and resource_type not in resource_types_in_module):
                continue

            changes.append(GAChange(
                change_type=change_type,
                resource_type=resource_type,
                attribute_name=attribute,
                description=line[:300],
                provider_version=target_version,
                breaking=_is_breaking(line),
                source_url=f"https://github.com/hashicorp/terraform-provider-google/blob/main/CHANGELOG.md",
            ))
            break  # one match per line

    return changes


def _is_breaking(line: str) -> bool:
    """Heuristic: is this change a breaking change?"""
    BREAKING_KEYWORDS = [
        "breaking", "removed", "no longer", "deprecated and removed",
        "must now", "required", "renamed to",
    ]
    lower = line.lower()
    return any(kw in lower for kw in BREAKING_KEYWORDS)


# ── LLM-powered change analysis ───────────────────────────────────────────────

async def analyze_changes_with_llm(
    changelog_text: str,
    module_summary: dict,
    run: WorkflowRun,
) -> list[GAChange]:
    """
    Use the local LLM to do a deeper analysis of changes, mapping them
    to specific files and producing implementation guidance.
    Falls back to regex parsing if LLM is unavailable.
    """
    cfg = get_config()
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        base_url=cfg.llm.base_url.rstrip("/") + "/v1",
        api_key="ollama",
    )

    prompt = f"""You are a Terraform module expert. Analyze this Google provider changelog and identify
all changes relevant to this Terraform module.

MODULE SUMMARY:
{module_summary}

CHANGELOG EXCERPT:
{changelog_text[:4000]}

Return a JSON array of change objects. Each object must have exactly these fields:
- change_type: one of: new_resource, new_argument, deprecated_argument, new_variable, updated_variable, provider_version, new_output, iam_change, api_requirement, lifecycle_change
- resource_type: the google_xxx resource type (e.g. "google_bigquery_dataset")
- attribute_name: the specific field/argument name, or null
- description: one sentence describing the change
- provider_version: the provider version string
- breaking: true or false
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
        # Strip markdown fences if present
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("```").strip()

        import json
        items = json.loads(raw)
        changes = []
        for item in items:
            try:
                changes.append(GAChange(**item))
            except Exception:
                pass
        run.log(f"LLM identified {len(changes)} relevant changes")
        return changes
    except Exception as e:
        run.log(f"LLM analysis failed ({e}), falling back to regex parser", level="warning")
        return []


# ── Main entry point ──────────────────────────────────────────────────────────

async def detect_ga_release(
    repo_name: str,
    run: WorkflowRun,
    github_token: Optional[str] = None,
) -> Optional[GAChangeSet]:
    """
    Full GA detection pipeline:
    1. Get current provider version from versions.tf
    2. Fetch latest GA version from Terraform Registry
    3. Download and parse changelog between the two versions
    4. Use LLM to map changes to module resources
    5. Return a structured GAChangeSet
    """
    cfg = get_config()
    repo_cfg = cfg.get_repo(repo_name)
    if not repo_cfg:
        run.fail(f"Repo '{repo_name}' not found in config")
        return None

    run.stage = WorkflowStage.DETECTING
    run.log(f"Detecting GA release for {repo_name}")

    # Get current module tag and provider version
    current_tag = get_latest_tag(repo_name) or "main"
    run.log(f"Analyzing current tag: {current_tag}")

    current_version = get_current_provider_version(repo_name, current_tag)
    if not current_version:
        run.log("Could not determine current provider version — defaulting to '4.0.0'", level="warning")
        current_version = "4.0.0"

    run.log(f"Current provider version: {current_version}")

    # Fetch latest GA version
    latest_version = await fetch_latest_ga_version()
    if not latest_version:
        run.log("Could not fetch latest GA version from Terraform Registry", level="warning")
        latest_version = current_version

    run.log(f"Latest GA version: {latest_version}")

    upgrade_required = _semver_tuple(latest_version) > _semver_tuple(current_version)

    ga_release = GARelease(
        current_version=current_version,
        latest_ga_version=latest_version,
        upgrade_required=upgrade_required,
        changelog_url=f"https://github.com/hashicorp/terraform-provider-google/blob/main/CHANGELOG.md",
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
            summary="Module is already on the latest GA provider version. No changes required.",
        )

    # Fetch changelog
    run.log(f"Fetching changelog from v{current_version} to v{latest_version}")
    changelog_text = await fetch_provider_changelog(current_version, latest_version)

    # Also try GitHub release notes for the latest version
    release_notes = await fetch_github_release_notes(latest_version, github_token)
    if release_notes:
        changelog_text = f"{release_notes}\n\n{changelog_text}"

    # Get resource types used in the module
    resources = get_all_resources(repo_name, current_tag)
    resource_types = list({r.resource_type for r in resources})
    run.log(f"Module uses {len(resource_types)} resource types")

    # Build module summary for LLM context
    from backend.agent.tools.hcl_tools import summarize_module
    module_summary = summarize_module(repo_name, current_tag)

    # LLM-powered analysis
    run.stage = WorkflowStage.ANALYZING
    llm_changes = await analyze_changes_with_llm(changelog_text, module_summary, run)

    # Fallback to regex parsing if LLM returned nothing
    if not llm_changes:
        regex_changes = parse_changelog_to_changes(changelog_text, resource_types, latest_version)
        changes = regex_changes
        run.log(f"Regex parser found {len(changes)} relevant changes")
    else:
        changes = llm_changes

    # Count breaking changes and features
    breaking = sum(1 for c in changes if c.breaking)
    ga_release.breaking_changes = breaking
    ga_release.new_features = len(changes) - breaking

    # Determine which files need modification
    tf_files = list_tf_files_at_tag(repo_name, current_tag)
    files_to_modify = _map_changes_to_files(changes, tf_files)

    summary = _build_summary(ga_release, changes, repo_cfg.gcp_product)

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
    """Parse '4.12.3' into (4, 12, 3) for comparison."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", str(version))
    if m:
        return tuple(int(x) for x in m.groups())
    return (0, 0, 0)


def _extract_changelog_range(full_text: str, from_ver: str, to_ver: str) -> str:
    """Extract changelog entries for versions between from_ver and to_ver."""
    lines = full_text.splitlines()
    capturing = False
    result_lines = []

    from_tuple = _semver_tuple(from_ver)
    to_tuple = _semver_tuple(to_ver)

    for line in lines:
        # Check if this line is a version header
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
    # Cap to avoid overwhelming the LLM context
    return result[:8000] if result else full_text[:4000]


def _map_changes_to_files(changes: list[GAChange], tf_files: list[str]) -> list[str]:
    """Determine which .tf files are likely affected by the changes."""
    affected = set()
    for change in changes:
        resource = change.resource_type.lower()
        for f in tf_files:
            fname = f.lower()
            # Heuristic: main.tf always, plus resource-named files
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


def _build_summary(release: GARelease, changes: list[GAChange], product: str) -> str:
    if not changes:
        return f"No changes detected for the {product} module between provider v{release.current_version} and v{release.latest_ga_version}."

    breaking = [c for c in changes if c.breaking]
    features = [c for c in changes if not c.breaking]

    parts = [
        f"Google provider upgrade from v{release.current_version} to v{release.latest_ga_version} "
        f"for the {product} module introduces {len(changes)} changes: "
        f"{len(features)} new features and {len(breaking)} breaking changes."
    ]
    if breaking:
        parts.append(f"Breaking: {'; '.join(c.description[:80] for c in breaking[:3])}.")
    return " ".join(parts)
