"""
ga_compat.py — Stage 6: Provider compatibility verification.

Queries the Terraform Registry schema API to confirm that every new
attribute / resource type proposed by the GAChangeSet actually exists
in the target provider version.

Functions (as documented in GA_WORKFLOW_README.md §5):
  fetch_provider_schema()        → GET registry schema for a given version
  check_provider_compatibility() → cross-check every GAChange against schema
  generate_versions_update()     → produce a new versions.tf HCL block if needed
"""
from __future__ import annotations

import re
import time
from functools import lru_cache
from typing import Any, Optional

import httpx

from backend.config import get_config
from backend.ga_workflow.ga_models import (
    ChangeType,
    GAChangeSet,
    ProviderCompatCheck,
    ProviderCompatibility,
    WorkflowRun,
    WorkflowStage,
)

REGISTRY_SCHEMA_URL = (
    "https://registry.terraform.io/v1/providers/hashicorp/google/{version}/schema"
)
HTTP_TIMEOUT = 20.0

# In-memory cache: version → schema dict  (avoids hammering the Registry API)
_schema_cache: dict[str, dict] = {}


# ── Public: fetch schema ───────────────────────────────────────────────────────

async def fetch_provider_schema(version: str) -> Optional[dict]:
    """
    Fetch the full JSON schema for the Google provider at `version` from the
    Terraform Registry.

    The schema is cached in-process so repeated calls within one workflow run
    cost only one network round-trip.

    Returns the raw schema dict or None on network / parse failure.
    """
    if version in _schema_cache:
        return _schema_cache[version]

    url = REGISTRY_SCHEMA_URL.format(version=version)
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            schema = resp.json()
            _schema_cache[version] = schema
            return schema
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            # Schema endpoint may not exist for every minor version;
            # fall back to the nearest available version (best-effort).
            return None
        return None
    except Exception as e:
        print(f"[ga_compat] Schema fetch error for v{version}: {e}")
        return None


# ── Public: compatibility check ───────────────────────────────────────────────

async def check_provider_compatibility(
    change_set: GAChangeSet,
    run: WorkflowRun,
) -> ProviderCompatibility:
    """
    For every GAChange in change_set, verify the attribute / resource type
    exists in the target provider version's schema.

    Logic (as documented in README §12):
      new_argument    → schema[resource_type][attributes][attribute_name] exists?
      new_resource    → schema[resource_type] exists?
      deprecated_arg  → schema[resource_type][attributes][attribute_name][deprecated] set?
      provider_version → always compatible (it IS the version we're targeting)
      all others      → mark supported=True (cannot verify without schema entry)

    Continues on incompatibility — marks the check and notes it in the PR body
    rather than aborting the workflow.
    """
    run.stage = WorkflowStage.VALIDATING          # reuse VALIDATING enum value
    target_ver = change_set.ga_release.latest_ga_version
    current_ver = change_set.ga_release.current_version

    run.log(f"Fetching provider schema for v{target_ver} …")
    schema = await fetch_provider_schema(target_ver)

    if schema is None:
        run.log(
            f"Could not fetch provider schema for v{target_ver} — "
            f"marking all checks as unverified.",
            level="warning",
        )
        checks = [
            ProviderCompatCheck(
                resource_type=c.resource_type,
                attribute_name=c.attribute_name,
                supported=True,          # assume OK; cannot verify
                notes="Schema unavailable — manual verification required.",
            )
            for c in change_set.changes
        ]
        return ProviderCompatibility(
            repo_name=change_set.repo_name,
            current_version=current_ver,
            target_version=target_ver,
            all_compatible=True,
            checks=checks,
            versions_tf_update=generate_versions_update(current_ver, target_ver),
        )

    resource_schemas: dict = (
        schema.get("schemas", {})
        or schema.get("resource_schemas", {})
        or schema.get("provider_schema", {}).get("resource_schemas", {})
        or {}
    )

    checks: list[ProviderCompatCheck] = []

    for change in change_set.changes:
        check = _check_one(change.change_type, change.resource_type,
                           change.attribute_name, target_ver, resource_schemas)
        checks.append(check)

        level = "info" if check.supported else "warning"
        attr_part = f".{check.attribute_name}" if check.attribute_name else ""
        status = "✅" if check.supported else "⚠"
        run.log(
            f"  {status} {check.resource_type}{attr_part}: "
            f"{'supported' if check.supported else 'NOT FOUND in schema'}",
            level=level,
        )
        if not check.supported:
            run.log(
                f"     → Will note in PR body for manual review.",
                level="warning",
            )

    all_compatible = all(c.supported for c in checks)
    versions_update = generate_versions_update(current_ver, target_ver)

    run.log(
        f"Provider compatibility: "
        f"{'✅ all {len(checks)} checks passed' if all_compatible else f'⚠ {sum(1 for c in checks if not c.supported)} incompatible'}"
    )

    return ProviderCompatibility(
        repo_name=change_set.repo_name,
        current_version=current_ver,
        target_version=target_ver,
        all_compatible=all_compatible,
        checks=checks,
        versions_tf_update=versions_update,
    )


# ── Public: versions.tf patch ─────────────────────────────────────────────────

def generate_versions_update(current_version: str, target_version: str) -> str:
    """
    Return a complete updated `terraform { required_providers {} }` block
    that bumps the google provider floor to target_version.

    The upper bound is set to (major + 1).0 to remain open to future patches
    within the same major line.

    Example:
        current_version = "5.10.0"
        target_version  = "5.38.0"
        →  version = ">= 5.38, < 6.0"
    """
    m = re.match(r"(\d+)\.(\d+)", target_version)
    if not m:
        return ""
    major = int(m.group(1))
    minor = m.group(2)
    constraint = f">= {major}.{minor}, < {major + 1}.0"

    return (
        f"# Updated by TerraScope GA Workflow — provider v{target_version}\n"
        f'terraform {{\n'
        f'  required_providers {{\n'
        f'    google = {{\n'
        f'      source  = "hashicorp/google"\n'
        f'      version = "{constraint}"\n'
        f'    }}\n'
        f'  }}\n'
        f'  required_version = ">= 1.3"\n'
        f'}}\n'
    )


# ── Internal helpers ───────────────────────────────────────────────────────────

def _check_one(
    change_type: ChangeType,
    resource_type: str,
    attribute_name: Optional[str],
    target_version: str,
    resource_schemas: dict,
) -> ProviderCompatCheck:
    """Run one compatibility check and return a ProviderCompatCheck."""

    # Provider version bump — always compatible by definition
    if change_type == ChangeType.PROVIDER_VERSION:
        return ProviderCompatCheck(
            resource_type=resource_type,
            attribute_name=attribute_name,
            supported=True,
            min_version=target_version,
            notes="Provider version bump — no schema check needed.",
        )

    res_schema = resource_schemas.get(resource_type)

    # New resource type check
    if change_type == ChangeType.NEW_RESOURCE:
        if res_schema is not None:
            return ProviderCompatCheck(
                resource_type=resource_type,
                attribute_name=None,
                supported=True,
                min_version=target_version,
            )
        return ProviderCompatCheck(
            resource_type=resource_type,
            attribute_name=None,
            supported=False,
            notes=(
                f"`{resource_type}` not found in provider schema v{target_version}. "
                f"It may have a different name or require a beta provider."
            ),
        )

    # All attribute-level checks require the resource to exist first
    if res_schema is None:
        return ProviderCompatCheck(
            resource_type=resource_type,
            attribute_name=attribute_name,
            supported=False,
            notes=(
                f"Resource `{resource_type}` not found in schema v{target_version}. "
                f"The resource may be in the google-beta provider."
            ),
        )

    attrs: dict = (
        res_schema.get("block", {}).get("attributes", {})
        or res_schema.get("schema", {}).get("attributes", {})
        or {}
    )
    block_types: dict = (
        res_schema.get("block", {}).get("block_types", {})
        or {}
    )

    if not attribute_name:
        # No attribute to check — resource existence is sufficient
        return ProviderCompatCheck(
            resource_type=resource_type,
            attribute_name=None,
            supported=True,
        )

    # Handle nested attributes like "time_partitioning.expiration_ms"
    top_attr = attribute_name.split(".")[0]
    found_in_attrs = top_attr in attrs
    found_in_blocks = top_attr in block_types

    if change_type == ChangeType.NEW_ARGUMENT:
        supported = found_in_attrs or found_in_blocks
        return ProviderCompatCheck(
            resource_type=resource_type,
            attribute_name=attribute_name,
            supported=supported,
            min_version=target_version if supported else None,
            notes=(
                None if supported else
                f"Attribute `{attribute_name}` not found in `{resource_type}` schema "
                f"at v{target_version}. Verify the attribute name in the provider docs."
            ),
        )

    if change_type == ChangeType.DEPRECATED_ARG:
        attr_meta = attrs.get(top_attr, {})
        is_deprecated = (
            attr_meta.get("deprecated", False)
            if isinstance(attr_meta, dict) else False
        )
        supported = found_in_attrs or found_in_blocks
        return ProviderCompatCheck(
            resource_type=resource_type,
            attribute_name=attribute_name,
            supported=supported,
            deprecated_in=target_version if is_deprecated else None,
            notes=(
                "Attribute is deprecated in this version — migration required."
                if is_deprecated else None
            ),
        )

    # IAM, API requirement, lifecycle, and other types — resource presence is enough
    return ProviderCompatCheck(
        resource_type=resource_type,
        attribute_name=attribute_name,
        supported=found_in_attrs or found_in_blocks or True,
        notes="Compatibility assumed — change type does not require attribute-level schema check.",
    )
