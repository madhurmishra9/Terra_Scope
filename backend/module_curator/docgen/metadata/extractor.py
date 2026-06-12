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

def _apply_variables(parsed: dict, meta: TFModuleMetadata) -> None:
    for var_block in parsed.get("variable", []):
        for var_name, cfg in var_block.items():
            has_default = "default" in cfg
            iv = InputVariable(
                type=str(cfg.get("type", "any")),
                description=str(cfg.get("description", "")),
                default=str(cfg["default"]) if has_default else None,
                required=not has_default,
            )
            if iv.required:
                meta.required_inputs[var_name] = iv
            else:
                meta.optional_inputs[var_name] = iv


def _apply_outputs(parsed: dict, meta: TFModuleMetadata) -> None:
    for out_block in parsed.get("output", []):
        for out_name, cfg in out_block.items():
            meta.outputs[out_name] = OutputValue(
                description=str(cfg.get("description", "")),
                value=str(cfg.get("value", "")),
            )


def _apply_main(parsed: dict, meta: TFModuleMetadata) -> None:
    for resource_block in parsed.get("resource", []):
        for rtype in resource_block:
            if rtype not in meta.resource_types:
                meta.resource_types.append(rtype)
    for module_block in parsed.get("module", []):
        for mod_name in module_block:
            if mod_name not in meta.module_calls:
                meta.module_calls.append(mod_name)


def _apply_versions(parsed: dict, meta: TFModuleMetadata) -> None:
    for tf_block in parsed.get("terraform", []):
        # required_version
        req_tf = tf_block.get("required_version", "")
        if req_tf and not meta.terraform_version:
            meta.terraform_version = str(req_tf)
        # required_providers
        req_provs = tf_block.get("required_providers", [])
        if isinstance(req_provs, list):
            for rp in req_provs:
                if not isinstance(rp, dict):
                    continue
                for pname, pcfg in rp.items():
                    v = pcfg.get("version", "") if isinstance(pcfg, dict) else ""
                    if pname in ("google", "aws", "azurerm") and not meta.provider_version:
                        meta.provider = pname
                        meta.provider_version = str(v)


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
