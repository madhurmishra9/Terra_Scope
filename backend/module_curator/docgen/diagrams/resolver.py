"""
diagrams/resolver.py — Resolve diagram images per document type (Priority 1).

Keeps the previous resolve() call signature so pipeline.py's call site needs
minimal change, but:

  * Diagram truth comes from parsing the module's HCL directly (hcl_graph),
    not `terraform graph | dot` (no init needed, no provider/meta noise).
  * The model never draws pixels — mermaid source is generated, then rendered
    deterministically (Kroki or mmdc). A failed render is a FAILURE state,
    not a silent placeholder.
  * Folder-supplied images are still honoured, but source-form diagrams
    (.mmd) are preferred and parsed so conformance can be checked.
  * resolve() now returns a DiagramResult carrying `publishable`. The
    Confluence publish step MUST refuse to publish when publishable=False —
    that is the render gate. A placeholder can preview, never publish.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .hcl_graph import graph_from_hcl
from .mermaid_render import DiagramRenderError, render_mermaid_kroki_bytes

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp")
_AUTO_GEN_TYPES = {"HLD", "Architectural Design"}

_PLACEHOLDER_SVG = """\
<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="640" height="200">
  <rect width="640" height="200" fill="#f5f7fa" stroke="#d0d5dd" rx="4"/>
  <text x="320" y="85" font-family="Arial,sans-serif" font-size="16" fill="#475467"
        text-anchor="middle">{caption}</text>
  <text x="320" y="125" font-family="Arial,sans-serif" font-size="12" fill="#98a2b3"
        text-anchor="middle">[Placeholder — diagram could not be produced]</text>
</svg>"""


@dataclass
class DiagramResult:
    image_bytes: bytes
    ext: str
    publishable: bool                       # False => preview-only; publish must refuse
    mermaid_source: Optional[str] = None    # kept for README embedding
    problems: list[str] = field(default_factory=list)


def resolve(
    doc_type: str,
    caption: str,
    product_name: str,
    module_dir: Optional[Path] = None,
    assets_dir: Optional[Path] = None,
    kroki_url: str = "http://localhost:8000",
) -> DiagramResult:
    # 1. Folder-supplied SOURCE diagram (.mmd) — best: parseable + renderable
    if assets_dir:
        mmd = _find_source(assets_dir, product_name, doc_type)
        if mmd:
            try:
                png = render_mermaid_kroki_bytes(mmd, fmt="png", kroki_url=kroki_url)
                return DiagramResult(png, "png", True, mermaid_source=mmd)
            except DiagramRenderError as e:
                return DiagramResult(
                    _placeholder(caption or doc_type), "svg", False,
                    problems=[f"supplied .mmd failed render gate: {e}"],
                )

        # 2. Folder-supplied IMAGE — honoured, but flagged unverified
        img = _find_image(assets_dir, product_name, doc_type)
        if img:
            data, ext = img
            return DiagramResult(
                data, ext, True,
                problems=["image supplied without source — conformance not checked"],
            )

    # 3. Auto-generate from the module's HCL (architecture doc types)
    if doc_type in _AUTO_GEN_TYPES and module_dir and module_dir.is_dir():
        files = {
            p.name: p.read_text(encoding="utf-8", errors="ignore")
            for p in module_dir.glob("*.tf")
        }
        if files:
            graph = graph_from_hcl(files)
            if graph.nodes:
                src = graph.to_mermaid()
                try:
                    png = render_mermaid_kroki_bytes(src, fmt="png", kroki_url=kroki_url)
                    return DiagramResult(png, "png", True, mermaid_source=src)
                except DiagramRenderError as e:
                    return DiagramResult(
                        _placeholder(caption or doc_type), "svg", False,
                        mermaid_source=src,
                        problems=[f"render gate failed: {e}"],
                    )

    # 4. Placeholder — preview only, NEVER publishable
    return DiagramResult(
        _placeholder(caption or doc_type), "svg", False,
        problems=["no diagram source available"],
    )


def _find_source(assets_dir: Path, product: str, doc_type: str) -> Optional[str]:
    slug = doc_type.lower().replace(" ", "_")
    for folder in (assets_dir / product, assets_dir):
        cand = folder / f"{slug}.mmd"
        if cand.is_file():
            return cand.read_text(encoding="utf-8")
    return None


def _find_image(assets_dir: Path, product: str, doc_type: str):
    slug = doc_type.lower().replace(" ", "_")
    for folder in (assets_dir / product, assets_dir):
        if not folder.is_dir():
            continue
        for ext in _IMAGE_EXTS:
            cand = folder / f"{slug}{ext}"
            if cand.is_file():
                return cand.read_bytes(), ext.lstrip(".")
    return None


def _placeholder(caption: str) -> bytes:
    safe = caption.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _PLACEHOLDER_SVG.format(caption=safe).encode("utf-8")
