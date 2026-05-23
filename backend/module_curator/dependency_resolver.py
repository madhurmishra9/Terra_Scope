"""
dependency_resolver.py — Recursively resolve a Terraform module's dependencies.

Given a set of root .tf files, walks every `module "name" { source = "..." }`
block, resolves the source against (in order):

  1. Local relative paths  (./foo, ../foo)            → read from disk
  2. Local repos/ catalogue (./repos/<name>)          → local_repo_scanner
  3. ChromaDB indexed repos                           → semantic_search snippet
  4. Terraform Registry doc cache + scraper           → registry_api

Each resolved dependency is itself recursed into, with a depth cap and
cycle detection so a public-network registry module won't blow up.

Output: a flat list of ResolvedDependency objects, each carrying enough
context for the LLM to write a correct `module "x" { ... }` call:
  - source string (as the consumer should write it)
  - resolved kind (local / repos / chromadb / registry)
  - the module's input variables and outputs
  - a short text excerpt of the most relevant snippet

This module purposely does NOT mutate the session — the caller decides
how to use the dependency tree.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from backend.module_curator.local_repo_scanner import (
    LocalModule,
    resolve_source_locally,
    scan_local_modules,
)

_PROJECT_ROOT = Path(__file__).parent.parent.parent

_MODULE_BLOCK_RE = re.compile(
    r'module\s+"([^"]+)"\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}',
    re.DOTALL,
)
_SOURCE_LINE_RE = re.compile(r'source\s*=\s*"([^"]+)"')


# ── Output types ─────────────────────────────────────────────────────────────

@dataclass
class ResolvedDependency:
    """One resolved module reference."""

    raw_source:        str                            # exact string from the source = "..."
    kind:              str                            # local | repos | chromadb | registry | unresolved
    depth:             int                            # 0 = directly referenced from root
    parent_module:     str                            # which module declared this dependency
    local_module:      Optional[LocalModule] = None   # set when kind == "repos"
    local_path:        Optional[Path] = None          # set when kind == "local"
    snippet:           str = ""                       # text excerpt (registry/chromadb)
    inputs:            dict[str, dict] = field(default_factory=dict)
    outputs:           dict[str, str] = field(default_factory=dict)
    error:             Optional[str] = None

    def short_label(self) -> str:
        return f"[{self.kind}] {self.raw_source}"


@dataclass
class DependencyTree:
    """Result of resolving every dependency in a module."""

    dependencies: list[ResolvedDependency] = field(default_factory=list)
    unresolved:   list[str] = field(default_factory=list)
    cycle_detected: bool = False

    def by_kind(self, kind: str) -> list[ResolvedDependency]:
        return [d for d in self.dependencies if d.kind == kind]

    def required_inputs_for(self, source: str) -> dict[str, dict]:
        for d in self.dependencies:
            if d.raw_source == source:
                return {n: v for n, v in d.inputs.items() if v.get("required")}
        return {}

    def summary_for_prompt(self, max_modules: int = 6) -> str:
        if not self.dependencies:
            return ""
        lines: list[str] = []
        for d in self.dependencies[:max_modules]:
            req = list({n for n, v in d.inputs.items() if v.get("required")})[:6]
            outs = list(d.outputs.keys())[:5]
            head = f"- {d.short_label()}  (depth {d.depth})"
            lines.append(head)
            if req:
                lines.append(f"    required inputs: {', '.join(req)}")
            if outs:
                lines.append(f"    outputs:         {', '.join(outs)}")
            if d.snippet:
                lines.append(f"    excerpt: {d.snippet[:240]!r}")
        if len(self.dependencies) > max_modules:
            lines.append(f"  (+{len(self.dependencies) - max_modules} more)")
        if self.unresolved:
            lines.append(f"Unresolved sources: {', '.join(self.unresolved[:5])}")
        return "\n".join(lines)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_module_blocks(tf_files: dict[str, str]) -> list[tuple[str, str]]:
    """Return [(module_logical_name, source_value), ...] for every module {} block found."""
    results: list[tuple[str, str]] = []
    for content in tf_files.values():
        for m in _MODULE_BLOCK_RE.finditer(content):
            logical = m.group(1)
            body = m.group(2)
            sm = _SOURCE_LINE_RE.search(body)
            if sm:
                results.append((logical, sm.group(1).strip()))
    return results


def _load_tf_files_from_disk(path: Path, max_files: int = 30) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_dir():
        return out
    files = sorted(path.rglob("*.tf"))
    files = [f for f in files if "examples" not in f.parts and "test" not in f.parts]
    for tf in files[:max_files]:
        try:
            rel = str(tf.relative_to(path)).replace("\\", "/")
            out[rel] = tf.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
    return out


def _parse_inputs_outputs(tf_files: dict[str, str]) -> tuple[dict[str, dict], dict[str, str]]:
    """Lightweight reuse of the regex logic from local_repo_scanner for arbitrary tf_files."""
    from backend.module_curator.local_repo_scanner import (
        _VAR_RE, _OUTPUT_RE, _parse_variable_body, _parse_output_body,
    )
    inputs: dict[str, dict] = {}
    outputs: dict[str, str] = {}
    for content in tf_files.values():
        for m in _VAR_RE.finditer(content):
            name = m.group(1)
            if name not in inputs:
                inputs[name] = _parse_variable_body(m.group(2))
        for m in _OUTPUT_RE.finditer(content):
            outputs[m.group(1)] = _parse_output_body(m.group(2))
    return inputs, outputs


# ── Resolution strategies ────────────────────────────────────────────────────

def _resolve_local_path(
    source: str, anchor_dir: Path
) -> Optional[tuple[Path, dict[str, str]]]:
    """Resolve ./foo or ../foo against the directory that referenced it."""
    if not (source.startswith("./") or source.startswith("../")):
        return None
    target = (anchor_dir / source).resolve()
    if not target.is_dir():
        return None
    tf_files = _load_tf_files_from_disk(target)
    if not tf_files:
        return None
    return target, tf_files


def _try_chromadb(source: str) -> Optional[str]:
    """Return a snippet from ChromaDB for a module source, if any."""
    try:
        from backend.agent.tools.search_tools import semantic_search, get_indexed_tags
        from backend.config import get_config

        cfg = get_config()
        for repo in cfg.enabled_repos:
            tags = get_indexed_tags(repo.name)
            if not tags:
                continue
            results = semantic_search(
                f"module source {source}", repo.name, tags[0], n_results=2
            )
            if results:
                return "\n".join(r.snippet for r in results)[:1500]
    except Exception:
        pass
    return None


async def _try_registry(source: str) -> Optional[str]:
    """Map a source like 'terraform-google-modules/network/google' to registry docs."""
    parts = source.split("/")
    if len(parts) < 3:
        return None
    # Heuristic: use middle segment as the service name
    service_hint = parts[1]
    provider_hint = parts[2].split("//", 1)[0]
    if provider_hint not in ("google", "aws", "azurerm"):
        return None
    try:
        from backend.registry_fetcher.registry_api import fetch_service_docs
        docs = await fetch_service_docs(provider_hint, service_hint)
        if docs and not docs.startswith("No documentation") and not docs.startswith("[Offline"):
            return docs[:2000]
    except Exception:
        return None
    return None


# ── Public entry point ───────────────────────────────────────────────────────

async def resolve_dependencies(
    root_tf_files: dict[str, str],
    root_dir: Optional[Path] = None,
    max_depth: int = 2,
    max_total: int = 25,
) -> DependencyTree:
    """
    Walk every module {} reference in root_tf_files and resolve transitively.

    root_dir is used to anchor relative paths (./foo); if None, relative paths
    are skipped (they would have nowhere to resolve against).
    """
    tree = DependencyTree()
    if not root_tf_files:
        return tree

    # Make sure the repos scan has run at least once
    scan_local_modules()

    # Queue: (tf_files_to_scan, anchor_dir, depth, parent_label)
    queue: list[tuple[dict[str, str], Optional[Path], int, str]] = [
        (root_tf_files, root_dir, 0, "root")
    ]
    seen_sources: set[str] = set()

    while queue and len(tree.dependencies) < max_total:
        tf_files, anchor_dir, depth, parent = queue.pop(0)
        if depth > max_depth:
            continue

        for logical_name, source in _extract_module_blocks(tf_files):
            if source in seen_sources:
                tree.cycle_detected = True
                continue
            seen_sources.add(source)

            dep = ResolvedDependency(
                raw_source=source,
                kind="unresolved",
                depth=depth,
                parent_module=parent,
            )

            # Strategy 1: local relative path
            if anchor_dir and (source.startswith("./") or source.startswith("../")):
                resolved = _resolve_local_path(source, anchor_dir)
                if resolved:
                    target, sub_files = resolved
                    inputs, outputs = _parse_inputs_outputs(sub_files)
                    dep.kind = "local"
                    dep.local_path = target
                    dep.inputs = inputs
                    dep.outputs = outputs
                    tree.dependencies.append(dep)
                    queue.append((sub_files, target, depth + 1, source))
                    continue

            # Strategy 2: ./repos/ catalogue
            local_mod = resolve_source_locally(source)
            if local_mod:
                dep.kind = "repos"
                dep.local_module = local_mod
                dep.inputs = local_mod.inputs
                dep.outputs = local_mod.outputs
                tree.dependencies.append(dep)
                # Recurse into the local module's own deps
                queue.append((local_mod.tf_files, local_mod.abs_path, depth + 1, source))
                continue

            # Strategy 3: ChromaDB
            snippet = _try_chromadb(source)
            if snippet:
                dep.kind = "chromadb"
                dep.snippet = snippet
                tree.dependencies.append(dep)
                continue

            # Strategy 4: Registry
            try:
                reg_snippet = await _try_registry(source)
            except Exception:
                reg_snippet = None
            if reg_snippet:
                dep.kind = "registry"
                dep.snippet = reg_snippet
                tree.dependencies.append(dep)
                continue

            # Unresolved
            dep.error = "Source not found in local repos, ChromaDB, or Registry"
            tree.unresolved.append(source)
            tree.dependencies.append(dep)

    return tree
