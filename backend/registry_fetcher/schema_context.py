"""
schema_context.py — Turn a provider schema into a compact, prompt-injectable
"allowed arguments" block so the LLM generates against the real schema instead
of guessing.

Design goal: fit inside an 8K-context model. We therefore emit a *terse* spec:
  - required attributes with their type (the model MUST set these)
  - optional top-level attribute names (names only — no descriptions)
  - nested block names (names only)
Everything is hard-capped by a character budget so Pass A never overflows.

Public API:
  validate_resource_types(provider, candidate_types) -> list[str]
  build_schema_constraint_block(provider, resource_types, char_budget) -> str
"""
from __future__ import annotations

from typing import Optional

from backend.registry_fetcher.schema_fetcher import (
    fetch_provider_schema,
    _extract_resource_schemas,
)

# hcl2 primitive rendering for the "type" hints we surface to the model
_TYPE_HINT_MAX = 40


def _res_block(res_schema: dict) -> dict:
    """Return the inner `block` dict regardless of registry response shape."""
    return res_schema.get("block", {}) or res_schema.get("schema", {}) or {}


def _render_type(type_field: object) -> str:
    """Render a provider schema `type` field into a short human hint."""
    if isinstance(type_field, str):
        return type_field[:_TYPE_HINT_MAX]
    if isinstance(type_field, list) and type_field:
        # e.g. ["list", "string"] or ["map", "string"] or ["object", {...}]
        head = str(type_field[0])
        if len(type_field) > 1 and isinstance(type_field[1], str):
            return f"{head}({type_field[1]})"[:_TYPE_HINT_MAX]
        return head[:_TYPE_HINT_MAX]
    return "any"


def _summarise_resource(res_type: str, res_schema: dict) -> str:
    """Produce a compact spec for a single resource type."""
    block = _res_block(res_schema)
    attrs: dict = block.get("attributes", {}) or {}
    block_types: dict = block.get("block_types", {}) or {}

    required: list[str] = []
    optional: list[str] = []
    for name, spec in attrs.items():
        if not isinstance(spec, dict):
            optional.append(name)
            continue
        # computed-only attributes can't be set by the user — skip as inputs
        if spec.get("computed") and not spec.get("optional") and not spec.get("required"):
            continue
        if spec.get("required"):
            required.append(f"{name}:{_render_type(spec.get('type'))}")
        else:
            optional.append(name)

    lines = [f"### {res_type}"]
    if required:
        lines.append("  required: " + ", ".join(sorted(required)))
    if optional:
        lines.append("  optional: " + ", ".join(sorted(optional)))
    if block_types:
        # mark which nested blocks are required (min_items >= 1)
        nb: list[str] = []
        for bname, bspec in block_types.items():
            min_items = bspec.get("min_items", 0) if isinstance(bspec, dict) else 0
            nb.append(f"{bname}{{}}{'*' if min_items else ''}")
        lines.append("  nested blocks: " + ", ".join(sorted(nb)))
    return "\n".join(lines)


async def validate_resource_types(
    provider: str, candidate_types: list[str]
) -> list[str]:
    """Drop any candidate resource types that don't exist in the provider schema.

    Used to sanitise the planning-pass output BEFORE we inject schema, so the
    model never sees (or anchors on) a hallucinated resource type.
    """
    schema = await fetch_provider_schema(provider)
    if schema is None:
        # Offline: can't verify — return candidates unchanged, deduped.
        return list(dict.fromkeys(candidate_types))
    known = _extract_resource_schemas(schema)
    out: list[str] = []
    seen: set[str] = set()
    for rt in candidate_types:
        rt = rt.strip()
        if rt and rt in known and rt not in seen:
            out.append(rt)
            seen.add(rt)
    return out


async def build_schema_constraint_block(
    provider: str,
    resource_types: list[str],
    char_budget: int = 3000,
) -> str:
    """Build the schema constraint text injected into Pass A.

    Returns "" if the schema is unavailable (offline) so the caller can fall
    back to registry_docs. Hard-capped at char_budget to protect the 8K window.
    """
    if not resource_types:
        return ""
    schema = await fetch_provider_schema(provider)
    if schema is None:
        return ""

    res_schemas = _extract_resource_schemas(schema)
    chunks: list[str] = []
    used = 0
    for rt in resource_types:
        res_schema = res_schemas.get(rt)
        if not res_schema:
            continue
        chunk = _summarise_resource(rt, res_schema)
        if used + len(chunk) > char_budget:
            # Budget exhausted — stop rather than truncate mid-resource.
            chunks.append(f"### {rt}\n  (schema omitted — budget reached; "
                          f"consult provider docs)")
            break
        chunks.append(chunk)
        used += len(chunk) + 1

    if not chunks:
        return ""

    header = (
        "## AUTHORITATIVE PROVIDER SCHEMA (source of truth)\n"
        "Use ONLY the arguments listed below for each resource. Set every "
        "`required:` argument. Arguments marked with `{}` are nested blocks. "
        "A `*` marks a required nested block. Do NOT invent arguments that are "
        "not listed here.\n"
    )
    return header + "\n".join(chunks)
