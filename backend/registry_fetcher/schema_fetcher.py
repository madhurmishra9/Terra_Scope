"""
schema_fetcher.py — Fetch and cache Terraform provider schemas from registry.terraform.io.

Supports GCP (google), AWS (aws), and Azure (azurerm).
Schemas are cached in-process to avoid repeated network round-trips.
"""
from __future__ import annotations

import re
from typing import Optional

import httpx

REGISTRY_BASE = "https://registry.terraform.io/v1/providers"
HTTP_TIMEOUT = 20.0

PROVIDER_SLUGS: dict[str, str] = {
    "google":  "hashicorp/google",
    "aws":     "hashicorp/aws",
    "azurerm": "hashicorp/azurerm",
}

# In-memory caches
_version_cache: dict[str, str] = {}            # provider → latest GA version
_schema_cache: dict[str, Optional[dict]] = {}  # provider → full schema dict (None = fetch failed)


# ── Version fetching ──────────────────────────────────────────────────────────

async def fetch_latest_provider_version(provider: str) -> Optional[str]:
    """Return the latest GA (non-prerelease) version string for the provider."""
    if provider in _version_cache:
        return _version_cache[provider]

    slug = PROVIDER_SLUGS.get(provider)
    if not slug:
        return None

    url = f"{REGISTRY_BASE}/{slug}/versions"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        print(f"[schema_fetcher] Version fetch failed for {provider}: {exc}")
        return None

    versions = [
        v["version"]
        for v in data.get("versions", [])
        if not re.search(r"(alpha|beta|rc)", v.get("version", ""), re.I)
    ]
    if not versions:
        return None

    def _ver_key(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.sub(r"[^0-9.]", "", v).split(".") if x)

    latest = max(versions, key=_ver_key)
    _version_cache[provider] = latest
    print(f"[schema_fetcher] Latest GA version for {provider}: {latest}")
    return latest


# ── Schema fetching ───────────────────────────────────────────────────────────

async def fetch_provider_schema(provider: str) -> Optional[dict]:
    """Fetch the full JSON schema for the latest GA version of a provider.

    Cached in-process: subsequent calls within the same server lifetime are free.
    Returns None if offline or the provider is unsupported.
    """
    if provider in _schema_cache:
        return _schema_cache[provider]

    slug = PROVIDER_SLUGS.get(provider)
    if not slug:
        _schema_cache[provider] = None
        return None

    version = await fetch_latest_provider_version(provider)
    if not version:
        _schema_cache[provider] = None
        return None

    url = f"{REGISTRY_BASE}/{slug}/{version}/schema"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            schema = resp.json()
            _schema_cache[provider] = schema
            print(f"[schema_fetcher] Loaded {provider} provider schema v{version}")
            return schema
    except Exception as exc:
        print(f"[schema_fetcher] Schema fetch failed for {provider} v{version}: {exc}")
        _schema_cache[provider] = None
        return None


# ── Schema introspection helpers ──────────────────────────────────────────────

def _extract_resource_schemas(schema: dict) -> dict:
    """Normalise the registry response to a resource_type → schema dict.

    The registry has used at least three different JSON shapes over time;
    this handles all known variants.
    """
    return (
        schema.get("schemas", {})
        or schema.get("resource_schemas", {})
        or schema.get("provider_schema", {}).get("resource_schemas", {})
        or {}
    )


def get_resource_types(schema: dict) -> set[str]:
    """Return all known resource type names for this provider's schema."""
    return set(_extract_resource_schemas(schema).keys())


def check_resource_type(schema: dict, resource_type: str) -> bool:
    """Return True if resource_type exists in the provider schema."""
    return resource_type in _extract_resource_schemas(schema)


def check_attribute(schema: dict, resource_type: str, attr_path: str) -> bool:
    """Return True if attr_path is a known attribute/block for the resource.

    Supports dotted paths for nested blocks (e.g. 'time_partitioning.expiration_ms').
    An unknown resource type always returns False.
    """
    res_schema = _extract_resource_schemas(schema).get(resource_type)
    if res_schema is None:
        return False

    parts = attr_path.split(".")
    current: dict = res_schema

    for part in parts:
        attrs: dict = (
            current.get("block", {}).get("attributes", {})
            or current.get("schema", {}).get("attributes", {})
            or {}
        )
        block_types: dict = (
            current.get("block", {}).get("block_types", {})
            or {}
        )
        if part in attrs:
            current = attrs[part]
        elif part in block_types:
            current = block_types[part]
        else:
            return False

    return True


def clear_cache() -> None:
    """Clear all in-process caches (used in tests)."""
    _version_cache.clear()
    _schema_cache.clear()
