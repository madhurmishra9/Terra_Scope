"""
diagrams/resolver.py — Resolve diagram images per document type (Milestone 4).

Resolution order (per diagram type)
------------------------------------
1. Folder-supplied image — look in <assets_dir>/<product>/<type_slug>.<ext>
2. Auto-generate from `terraform graph | dot -Tpng` (HLD / Architectural only)
3. SVG placeholder labelled with caption + TODO note

The function never raises — failures fall through to the next strategy.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp")

_PLACEHOLDER_SVG = """\
<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="640" height="200">
  <rect width="640" height="200" fill="#f5f7fa" stroke="#d0d5dd" stroke-width="1" rx="4"/>
  <text x="320" y="85" font-family="Arial,sans-serif" font-size="16" fill="#475467"
        text-anchor="middle" dominant-baseline="middle">{caption}</text>
  <text x="320" y="125" font-family="Arial,sans-serif" font-size="12" fill="#98a2b3"
        text-anchor="middle" dominant-baseline="middle"
        >[Placeholder — replace with the actual diagram image]</text>
</svg>"""

# Doc types that can be auto-generated from the TF graph
_AUTO_GEN_TYPES = {"HLD", "Architectural Design"}


def resolve(
    doc_type: str,
    caption: str,
    product_name: str,
    module_dir: Optional[Path] = None,
    assets_dir: Optional[Path] = None,
) -> tuple[bytes, str]:
    """
    Return (image_bytes, file_extension) for the given *doc_type*.
    Falls back to an SVG placeholder on every failure.
    """
    # 1. Folder-supplied image (works for all types)
    if assets_dir:
        result = _find_in_folder(assets_dir, product_name, doc_type)
        if result:
            return result

    # 2. Auto-generate from `terraform graph` for architecture diagrams
    if doc_type in _AUTO_GEN_TYPES and module_dir:
        png = _terraform_graph_png(module_dir)
        if png:
            return png, "png"

    # 3. SVG placeholder
    return _placeholder(caption or doc_type), "svg"


# ── Strategies ────────────────────────────────────────────────────────────────

def _find_in_folder(
    assets_dir: Path,
    product_name: str,
    doc_type: str,
) -> Optional[tuple[bytes, str]]:
    """Search <assets_dir>/<product>/ then <assets_dir>/ for a matching image."""
    slug = doc_type.lower().replace(" ", "_")
    search_dirs = [assets_dir / product_name, assets_dir]
    for folder in search_dirs:
        if not folder.is_dir():
            continue
        for ext in _IMAGE_EXTS:
            candidate = folder / f"{slug}{ext}"
            if candidate.is_file():
                return candidate.read_bytes(), ext.lstrip(".")
    return None


def _terraform_graph_png(module_dir: Path) -> Optional[bytes]:
    """Run `terraform graph | dot -Tpng` and return PNG bytes, or None."""
    try:
        graph = subprocess.run(
            ["terraform", "graph"],
            capture_output=True, text=True,
            cwd=str(module_dir), timeout=30,
        )
        if graph.returncode != 0:
            return None
        dot = subprocess.run(
            ["dot", "-Tpng"],
            input=graph.stdout, capture_output=True, timeout=30,
        )
        return dot.stdout if dot.returncode == 0 and dot.stdout else None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _placeholder(caption: str) -> bytes:
    safe = caption.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _PLACEHOLDER_SVG.format(caption=safe).encode("utf-8")
