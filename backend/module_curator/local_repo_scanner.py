"""
local_repo_scanner.py — Scan the ./repos/ folder for matching Terraform modules.

This module makes the ./repos/ folder a first-class generation source. It:
  - Walks every configured (and unconfigured) repo under ./repos/.
  - Parses each module to extract: provider(s) used, resource types defined,
    input variables, outputs, and any module {} sources it references.
  - Finds modules that match a given service name (case-insensitive partial match
    on repo name, display name, gcp_product, and on the resource types defined).
  - Resolves a `source = "..."` string against the local repo catalogue so
    dependent-module references can be satisfied from disk before falling
    back to ChromaDB or the Terraform Registry.

No ChromaDB indexing required — this works on a fresh checkout.

Public API:
  scan_local_modules()                  → list[LocalModule]
  find_matching_modules(service, prov)  → list[LocalModule]  (ranked best-first)
  resolve_source_locally(source)        → Optional[LocalModule]
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from backend.config import get_config


_PROJECT_ROOT = Path(__file__).parent.parent.parent
_REPOS_DIR = _PROJECT_ROOT / "repos"

# Cache the scan so we don't re-walk the disk on every session.
_scan_cache: Optional[list["LocalModule"]] = None


# ── Public dataclass ─────────────────────────────────────────────────────────

@dataclass
class LocalModule:
    """A Terraform module discovered on disk under ./repos/."""

    name: str                                       # repo dir name
    display_name: str                               # from config if available, else name
    abs_path: Path                                  # absolute path to module root
    rel_path: str                                   # path relative to project root, fwd slashes
    provider_names: set[str] = field(default_factory=set)   # {"google", "aws", "azurerm", ...}
    resource_types: set[str] = field(default_factory=set)   # {"google_bigquery_dataset", ...}
    inputs: dict[str, dict] = field(default_factory=dict)   # var_name → {type, default, required, description}
    outputs: dict[str, str] = field(default_factory=dict)   # output_name → description
    module_sources: list[str] = field(default_factory=list) # raw `source = "..."` values referenced
    config_source_url: Optional[str] = None                 # GitHub URL from terrascope.config.yaml if known
    gcp_product: str = "unknown"
    description: str = ""
    tf_files: dict[str, str] = field(default_factory=dict)  # filename → content (root-level .tf only)

    @property
    def required_inputs(self) -> dict[str, dict]:
        """Inputs without a default value — caller must supply these."""
        return {n: v for n, v in self.inputs.items() if v.get("required")}

    def summary_for_prompt(self, max_inputs: int = 8) -> str:
        """Compact string used in LLM prompts."""
        req = list(self.required_inputs.keys())[:max_inputs]
        opt = [n for n in self.inputs if n not in self.required_inputs][:max_inputs]
        res = sorted(self.resource_types)[:8]
        out = list(self.outputs.keys())[:8]
        return (
            f"Module: {self.name}\n"
            f"  Path:        {self.rel_path}\n"
            f"  Providers:   {', '.join(sorted(self.provider_names)) or 'none'}\n"
            f"  Resources:   {', '.join(res) or 'none'}\n"
            f"  Req inputs:  {', '.join(req) or 'none'}\n"
            f"  Opt inputs:  {', '.join(opt) or 'none'}\n"
            f"  Outputs:     {', '.join(out) or 'none'}"
        )


# ── HCL extraction (regex-based; intentionally lenient) ──────────────────────

_VAR_RE        = re.compile(r'variable\s+"([^"]+)"\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}', re.DOTALL)
_OUTPUT_RE     = re.compile(r'output\s+"([^"]+)"\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}', re.DOTALL)
_RESOURCE_RE   = re.compile(r'resource\s+"([^"]+)"\s+"([^"]+)"', re.MULTILINE)
_SOURCE_RE     = re.compile(r'source\s*=\s*"([^"]+)"', re.MULTILINE)
_REQ_PROV_RE   = re.compile(r'required_providers\s*\{(.*?)\}\s*\}', re.DOTALL)
_PROV_BLOCK_RE = re.compile(r'(\w+)\s*=\s*\{[^}]*?source\s*=\s*"hashicorp/([^"]+)"', re.DOTALL)
_TYPE_RE       = re.compile(r'type\s*=\s*([^\n#]+)')
_DEFAULT_RE    = re.compile(r'default\s*=', re.MULTILINE)
_DESC_RE       = re.compile(r'description\s*=\s*"([^"]*)"')


def _parse_variable_body(body: str) -> dict:
    type_match = _TYPE_RE.search(body)
    has_default = bool(_DEFAULT_RE.search(body))
    desc_match = _DESC_RE.search(body)
    return {
        "type":        (type_match.group(1).strip() if type_match else "string"),
        "required":    not has_default,
        "description": (desc_match.group(1).strip() if desc_match else ""),
    }


def _parse_output_body(body: str) -> str:
    desc = _DESC_RE.search(body)
    return desc.group(1).strip() if desc else ""


def _extract_module_metadata(tf_files: dict[str, str]) -> tuple[set, set, dict, dict, list]:
    """Return (providers, resources, inputs, outputs, module_sources) for a module."""
    providers: set[str] = set()
    resources: set[str] = set()
    inputs: dict[str, dict] = {}
    outputs: dict[str, str] = {}
    sources: list[str] = []

    for content in tf_files.values():
        # Providers from required_providers blocks
        for rp in _REQ_PROV_RE.finditer(content):
            for pm in _PROV_BLOCK_RE.finditer(rp.group(1)):
                providers.add(pm.group(2).lower())

        # Resource types
        for rm in _RESOURCE_RE.finditer(content):
            rtype = rm.group(1)
            resources.add(rtype)
            # Infer provider from resource prefix if not declared
            if "_" in rtype:
                prefix = rtype.split("_", 1)[0]
                if prefix in ("google", "aws", "azurerm", "kubernetes", "helm"):
                    providers.add(prefix)

        # Variables
        for vm in _VAR_RE.finditer(content):
            name = vm.group(1)
            if name not in inputs:
                inputs[name] = _parse_variable_body(vm.group(2))

        # Outputs
        for om in _OUTPUT_RE.finditer(content):
            outputs[om.group(1)] = _parse_output_body(om.group(2))

        # Module sources
        for sm in _SOURCE_RE.finditer(content):
            sources.append(sm.group(1))

    return providers, resources, inputs, outputs, sources


# ── Disk walker ──────────────────────────────────────────────────────────────

def _load_root_tf_files(module_dir: Path, max_files: int = 20) -> dict[str, str]:
    """Read .tf files at the module root (one level deep only — submodules excluded)."""
    out: dict[str, str] = {}
    if not module_dir.is_dir():
        return out
    for tf in sorted(module_dir.glob("*.tf"))[:max_files]:
        try:
            out[tf.name] = tf.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
    return out


def _load_module_at_path(module_dir: Path, max_files: int = 50) -> dict[str, str]:
    """Read .tf files recursively under module_dir (used for dependent submodules)."""
    out: dict[str, str] = {}
    if not module_dir.is_dir():
        return out
    files = sorted(module_dir.rglob("*.tf"))
    # Exclude examples/ and test/ — they are consumer-side, not module-side
    files = [f for f in files if "examples" not in f.parts and "test" not in f.parts]
    for tf in files[:max_files]:
        try:
            rel = str(tf.relative_to(module_dir)).replace("\\", "/")
            out[rel] = tf.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
    return out


def _try_read_origin_url(repo_dir: Path) -> Optional[str]:
    """Best-effort: read origin URL from .git/config without invoking git."""
    cfg_file = repo_dir / ".git" / "config"
    if not cfg_file.is_file():
        return None
    try:
        text = cfg_file.read_text(encoding="utf-8", errors="replace")
        m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', text, re.DOTALL)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return None


def _build_local_module(repo_dir: Path) -> Optional[LocalModule]:
    """Construct a LocalModule from a single directory under ./repos/."""
    name = repo_dir.name
    if name.startswith(".") or name.startswith("_"):
        return None
    if not repo_dir.is_dir():
        return None

    # Look for tf files at root first, then one level deep (some repos wrap module/)
    tf_root = _load_root_tf_files(repo_dir)
    search_root = repo_dir
    if not tf_root:
        for sub in sorted(repo_dir.iterdir()):
            if sub.is_dir() and any(sub.glob("*.tf")):
                tf_root = _load_root_tf_files(sub)
                search_root = sub
                break
    if not tf_root:
        return None  # not a Terraform module

    providers, resources, inputs, outputs, sources = _extract_module_metadata(tf_root)

    # Enrich from terrascope.config.yaml when the repo is listed there
    try:
        cfg = get_config()
        repo_cfg = cfg.get_repo(name)
    except Exception:
        repo_cfg = None

    display_name = repo_cfg.display_name if repo_cfg else name
    gcp_product  = repo_cfg.gcp_product if repo_cfg else "unknown"
    description  = repo_cfg.description if repo_cfg else ""
    origin_url   = _try_read_origin_url(repo_dir)

    try:
        rel = str(search_root.relative_to(_PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        rel = str(search_root)

    return LocalModule(
        name=name,
        display_name=display_name,
        abs_path=search_root,
        rel_path=rel,
        provider_names=providers,
        resource_types=resources,
        inputs=inputs,
        outputs=outputs,
        module_sources=sources,
        config_source_url=origin_url,
        gcp_product=gcp_product,
        description=description,
        tf_files=tf_root,
    )


# ── Public API ───────────────────────────────────────────────────────────────

def scan_local_modules(force: bool = False) -> list[LocalModule]:
    """Walk ./repos/ once, return every Terraform module found."""
    global _scan_cache
    if _scan_cache is not None and not force:
        return _scan_cache

    modules: list[LocalModule] = []
    if not _REPOS_DIR.is_dir():
        _scan_cache = modules
        return modules

    for child in sorted(_REPOS_DIR.iterdir()):
        if not child.is_dir():
            continue
        mod = _build_local_module(child)
        if mod:
            modules.append(mod)

    _scan_cache = modules
    return modules


def invalidate_cache() -> None:
    """Force the next scan to re-read disk. Useful for tests or after a clone."""
    global _scan_cache
    _scan_cache = None


_PROVIDER_TO_FAMILY = {
    "google":  "gcp",
    "aws":     "aws",
    "azurerm": "azure",
}


def _provider_matches(module: LocalModule, provider: str) -> bool:
    """True if the module declares (or uses) the requested provider."""
    if not module.provider_names:
        return True   # unknown — don't exclude
    return provider.lower() in module.provider_names


def _score_match(module: LocalModule, service: str, provider: str) -> int:
    """Higher score = better match. 0 = no match at all."""
    s = service.lower().strip()
    if not s:
        return 1 if _provider_matches(module, provider) else 0

    score = 0
    name_l = module.name.lower()
    disp_l = module.display_name.lower()
    prod_l = module.gcp_product.lower()
    desc_l = module.description.lower()

    # Strong: service token appears in repo identifiers
    tokens = [t for t in re.split(r"[\s_/-]+", s) if t]
    for t in tokens:
        if t in name_l:    score += 5
        if t in disp_l:    score += 3
        if t in prod_l:    score += 5
        if t in desc_l:    score += 2

    # Strongest: resource type contains the token
    for t in tokens:
        for rt in module.resource_types:
            if t in rt.lower():
                score += 6
                break

    # Provider alignment
    if _provider_matches(module, provider):
        score += 1
    else:
        # Different provider — heavily penalise but don't drop entirely
        score -= 5

    return score


def find_matching_modules(
    service_name: str,
    provider: str,
    limit: int = 5,
    min_score: int = 3,
) -> list[LocalModule]:
    """Return modules in repos/ that look relevant to (service_name, provider), best first."""
    all_mods = scan_local_modules()
    scored = [(m, _score_match(m, service_name, provider)) for m in all_mods]
    scored = [(m, sc) for m, sc in scored if sc >= min_score]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [m for m, _ in scored[:limit]]


# ── Source-string resolution ─────────────────────────────────────────────────

_GITHUB_URL_RE = re.compile(
    r"(?:git::)?(?:https://|git@)?github\.com[:/]([^/]+)/([^/.?#]+?)(?:\.git)?(?:[/?#].*)?$",
    re.IGNORECASE,
)


def _normalise_source(source: str) -> str:
    """Strip refs (?ref=...), git:: prefix, .git suffix."""
    s = source.split("?", 1)[0]
    s = s.split("//", 1)[0] if s.startswith(("git::", "http", "ssh")) and "//" in s[8:] else s
    if s.startswith("git::"):
        s = s[5:]
    if s.endswith(".git"):
        s = s[:-4]
    return s


def resolve_source_locally(source: str) -> Optional[LocalModule]:
    """
    Try to map a `source = "..."` value to a module in ./repos/.

    Resolution order:
      1. Direct repo name match (e.g. "terraform-google-bigquery").
      2. GitHub URL → match by org/repo name against scanned repos and their origin URLs.
      3. Registry-style "org/name/provider" → match the middle segment to a repo name.
    """
    s = _normalise_source(source).strip()
    if not s or s.startswith("./") or s.startswith("../"):
        return None

    modules = scan_local_modules()
    if not modules:
        return None

    # (1) Direct name
    for m in modules:
        if m.name == s or m.name.lower() == s.lower():
            return m

    # (2) GitHub URL
    gh = _GITHUB_URL_RE.match(s)
    if gh:
        repo_name = gh.group(2)
        for m in modules:
            if m.name.lower() == repo_name.lower():
                return m
            if m.config_source_url:
                ogh = _GITHUB_URL_RE.match(m.config_source_url)
                if ogh and ogh.group(2).lower() == repo_name.lower():
                    return m

    # (3) Registry shorthand: <org>/<name>/<provider>[//submodule]
    parts = s.split("/")
    if len(parts) >= 3 and "://" not in s:
        candidate = parts[1].lower()
        for m in modules:
            if candidate in m.name.lower() or m.name.lower() in candidate:
                return m

    return None


def get_module_by_name(name: str) -> Optional[LocalModule]:
    """Look up a scanned module by its directory name."""
    for m in scan_local_modules():
        if m.name == name:
            return m
    return None
