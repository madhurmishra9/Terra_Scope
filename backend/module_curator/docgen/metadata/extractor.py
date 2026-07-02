"""
metadata/extractor.py — Extract Terraform module metadata for field binding.

Two entry points
----------------
  extract_from_path(module_dir, service_name)
      Reads .tf files from a local directory via python-hcl2.

  extract_from_files(tf_files, service_name)
      Parses an in-memory {filename: hcl_content} dict (e.g. from a ZIP upload).

Both return a TFModuleMetadata Pydantic model consumed by template/fields.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import hcl2
from pydantic import BaseModel


class InputVariable(BaseModel):
    type:        str = "any"
    description: str = ""
    default:     Optional[str] = None
    required:    bool = True


class OutputValue(BaseModel):
    description: str = ""
    value:       str = ""


class TFModuleMetadata(BaseModel):
    service_name:      str
    provider:          str = "google"
    description:       str = ""
    resource_types:    list[str] = []
    data_source_types: list[str] = []
    required_inputs:   dict[str, InputVariable] = {}
    optional_inputs:   dict[str, InputVariable] = {}
    outputs:           dict[str, OutputValue]   = {}
    module_calls:      list[str] = []
    provider_version:  str = ""
    terraform_version: str = ""
    readme:            str = ""

    # ── Enrichment from Google official docs (populated by gcp_docs_fetcher) ──
    overview:                str = ""
    architecture_notes:      str = ""
    key_features:            list[str] = []
    use_cases:               list[str] = []
    terraform_design_considerations: list[str] = []
    iam_design:              list[str] = []
    security_considerations: list[str] = []
    limits_and_quotas:       list[str] = []
    cost_notes:              str = ""
    open_questions:          list[str] = []
    apis_required:           list[str] = []
    common_roles:            list[str] = []
    official_doc_urls:       list[str] = []

    @property
    def terraform_resource_refs(self) -> list[str]:
        """Per-resource/data-source reference links into the Terraform registry
        and the provider's GitHub source docs — so generated documents cite the
        authoritative Terraform documentation for every resource they mention.

        Computed (not stored) so it always reflects the current resource lists.
        Only the three hashicorp providers this app supports are linked; other
        prefixes are skipped rather than guessed (a wrong link is worse than none).
        """
        refs: list[str] = []
        provider = (self.provider or "google").strip().lower()
        if provider not in ("google", "aws", "azurerm"):
            return refs
        registry_base = f"https://registry.terraform.io/providers/hashicorp/{provider}/latest/docs"
        github_base = f"https://github.com/hashicorp/terraform-provider-{provider}/blob/main/website/docs"
        prefix = f"{provider}_"

        for rtype in self.resource_types:
            if not rtype.startswith(prefix):
                continue
            short = rtype[len(prefix):]
            refs.append(
                f"{rtype} — {registry_base}/resources/{short} · "
                f"source: {github_base}/r/{short}.html.markdown"
            )
        for dtype in self.data_source_types:
            if not dtype.startswith(prefix):
                continue
            short = dtype[len(prefix):]
            refs.append(
                f"data.{dtype} — {registry_base}/data-sources/{short} · "
                f"source: {github_base}/d/{short}.html.markdown"
            )
        return refs

    def merge_docs_bundle(self, bundle) -> None:
        """Fold a GCPDocsBundle's research fields into this metadata."""
        for attr in ("overview", "architecture_notes", "key_features", "use_cases",
                     "terraform_design_considerations", "iam_design",
                     "security_considerations", "limits_and_quotas", "cost_notes",
                     "open_questions", "apis_required", "common_roles",
                     "official_doc_urls"):
            val = getattr(bundle, attr, None)
            if val:
                setattr(self, attr, val)
        if bundle.overview and not self.description:
            self.description = bundle.overview


# ── Public API ────────────────────────────────────────────────────────────────

def extract_from_path(module_dir: Path, service_name: str = "") -> TFModuleMetadata:
    """Parse a local Terraform module directory."""
    meta = TFModuleMetadata(service_name=service_name or module_dir.name)

    for fname, handler in _FILE_HANDLERS.items():
        fpath = module_dir / fname
        if fpath.exists():
            try:
                parsed = hcl2.loads(fpath.read_text(encoding="utf-8"))
                handler(parsed, meta)
            except Exception:
                pass

    # Scan for any additional .tf files not matched above
    for tf_file in module_dir.glob("*.tf"):
        if tf_file.name not in _FILE_HANDLERS:
            try:
                parsed = hcl2.loads(tf_file.read_text(encoding="utf-8"))
                _apply_main(parsed, meta)   # pick up resources / module calls
            except Exception:
                pass

    _load_readme(module_dir, meta)
    return meta


def extract_from_files(tf_files: dict[str, str], service_name: str = "") -> TFModuleMetadata:
    """Parse an in-memory {filename: hcl_content} dict."""
    meta = TFModuleMetadata(service_name=service_name)

    for filename, content in tf_files.items():
        name = Path(filename).name.lower()
        try:
            parsed = hcl2.loads(content)
        except Exception:
            continue
        handler = _FILE_HANDLERS.get(name)
        if handler:
            handler(parsed, meta)
        else:
            _apply_main(parsed, meta)

    return meta


# ── Per-file handlers ─────────────────────────────────────────────────────────

def _unquote(label: str) -> str:
    """python-hcl2's string-mode parse leaves a block label's surrounding
    double quotes attached to the key (variable "project_id" -> the dict key
    is literally '"project_id"'), so every label must be stripped before use."""
    return str(label).strip().strip('"')


def _clean_value(v) -> str:
    """Strip the surrounding quotes hcl2 keeps on string literal VALUES."""
    s = str(v) if v is not None else ""
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1]
    return s


def _apply_variables(parsed: dict, meta: TFModuleMetadata) -> None:
    for var_block in parsed.get("variable", []):
        if not isinstance(var_block, dict):
            continue
        for var_name, cfg in var_block.items():
            if var_name == "__is_block__" or not isinstance(cfg, dict):
                continue
            has_default = "default" in cfg
            iv = InputVariable(
                type=_clean_value(cfg.get("type", "any")),
                description=_clean_value(cfg.get("description", "")),
                default=str(cfg["default"]) if has_default else None,
                required=not has_default,
            )
            if iv.required:
                meta.required_inputs[_unquote(var_name)] = iv
            else:
                meta.optional_inputs[_unquote(var_name)] = iv


def _apply_outputs(parsed: dict, meta: TFModuleMetadata) -> None:
    for out_block in parsed.get("output", []):
        if not isinstance(out_block, dict):
            continue
        for out_name, cfg in out_block.items():
            if out_name == "__is_block__" or not isinstance(cfg, dict):
                continue
            meta.outputs[_unquote(out_name)] = OutputValue(
                description=_clean_value(cfg.get("description", "")),
                value=str(cfg.get("value", "")),
            )


def _apply_main(parsed: dict, meta: TFModuleMetadata) -> None:
    for resource_block in parsed.get("resource", []):
        if not isinstance(resource_block, dict):
            continue
        for rtype in resource_block:
            if rtype == "__is_block__":
                continue
            rtype = _unquote(rtype)
            if rtype not in meta.resource_types:
                meta.resource_types.append(rtype)
    for data_block in parsed.get("data", []):
        if not isinstance(data_block, dict):
            continue
        for dtype in data_block:
            if dtype == "__is_block__":
                continue
            dtype = _unquote(dtype)
            if dtype not in meta.data_source_types:
                meta.data_source_types.append(dtype)
    for module_block in parsed.get("module", []):
        if not isinstance(module_block, dict):
            continue
        for mod_name in module_block:
            if mod_name == "__is_block__":
                continue
            mod_name = _unquote(mod_name)
            if mod_name not in meta.module_calls:
                meta.module_calls.append(mod_name)


def _apply_versions(parsed: dict, meta: TFModuleMetadata) -> None:
    for tf_block in parsed.get("terraform", []):
        if not isinstance(tf_block, dict):
            continue
        # required_version
        req_tf = tf_block.get("required_version", "")
        if req_tf and not meta.terraform_version:
            meta.terraform_version = _clean_value(req_tf)
        # required_providers
        req_provs = tf_block.get("required_providers", [])
        if isinstance(req_provs, list):
            for rp in req_provs:
                if not isinstance(rp, dict):
                    continue
                for pname, pcfg in rp.items():
                    pname = _unquote(pname)
                    v = pcfg.get("version", "") if isinstance(pcfg, dict) else ""
                    if pname in ("google", "aws", "azurerm") and not meta.provider_version:
                        meta.provider = pname
                        meta.provider_version = _clean_value(v)


_FILE_HANDLERS = {
    "variables.tf": _apply_variables,
    "variable.tf":  _apply_variables,
    "outputs.tf":   _apply_outputs,
    "output.tf":    _apply_outputs,
    "main.tf":      _apply_main,
    "versions.tf":  _apply_versions,
    "version.tf":   _apply_versions,
}


# ── README ────────────────────────────────────────────────────────────────────

def _load_readme(module_dir: Path, meta: TFModuleMetadata) -> None:
    for name in ("README.md", "readme.md", "README.txt", "readme.txt"):
        p = module_dir / name
        if p.exists():
            meta.readme = p.read_text(encoding="utf-8", errors="replace")
            if not meta.description:
                meta.description = _first_paragraph(meta.readme)
            return


def _first_paragraph(text: str) -> str:
    """Return the first non-empty, non-heading line from markdown text."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("<!--"):
            return stripped
    return ""
