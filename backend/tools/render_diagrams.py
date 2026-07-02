"""
tools/render_diagrams.py — Manual, on-demand diagram rendering.

Examples
--------
Render a standalone mermaid file to PNG (local mermaid-cli):
    python -m backend.tools.render_diagrams diagram.mmd -o diagram.png

Render via a self-hosted Kroki server (no local Node/Chromium needed):
    python -m backend.tools.render_diagrams diagram.mmd -o diagram.svg \\
        --backend kroki --kroki-url http://localhost:8000

Extract every mermaid block from a README and render each to ./assets:
    python -m backend.tools.render_diagrams README.md --from-readme -d assets --backend kroki

This is deliberately a manual step: diagrams live as mermaid source in the
README (GitHub renders them natively); you run this only when you need static
image assets for Confluence, the registry, or PDFs.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from backend.module_curator.docgen.diagrams.mermaid_render import (
    DiagramRenderError,
    render_diagram,
)
from backend.module_curator.docgen.diagrams.readme_embed import extract_mermaid_blocks


def _render_one(source: str, out_path: Path, backend: str, kroki_url: str) -> None:
    kwargs = {"kroki_url": kroki_url} if backend == "kroki" else {}
    render_diagram(source, out_path, backend=backend, **kwargs)
    print(f"  rendered -> {out_path}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="terrascope-render", description="Render mermaid diagrams to images on demand.")
    ap.add_argument("input", help="A .mmd file, or a README when --from-readme is set")
    ap.add_argument("-o", "--out", help="Output image path (single-diagram mode)")
    ap.add_argument("-d", "--out-dir", default=".", help="Output dir for --from-readme mode")
    ap.add_argument("--from-readme", action="store_true", help="Extract and render all mermaid blocks in the input file")
    ap.add_argument("--backend", choices=["mmdc", "kroki"], default="mmdc")
    ap.add_argument("--kroki-url", default="https://kroki.io")
    ap.add_argument("--format", default="png", help="Image format for --from-readme mode (png/svg/pdf)")
    args = ap.parse_args(argv)

    text = Path(args.input).read_text()

    try:
        if args.from_readme:
            blocks = extract_mermaid_blocks(text)
            if not blocks:
                print("No mermaid blocks found.", file=sys.stderr)
                return 1
            out_dir = Path(args.out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            stem = Path(args.input).stem
            print(f"Found {len(blocks)} diagram(s):")
            for i, src in enumerate(blocks, 1):
                out = out_dir / (f"{stem}-diagram-{i}.{args.format}" if len(blocks) > 1 else f"{stem}.{args.format}")
                _render_one(src, out, args.backend, args.kroki_url)
        else:
            if not args.out:
                print("Single-diagram mode needs -o/--out.", file=sys.stderr)
                return 2
            _render_one(text, Path(args.out), args.backend, args.kroki_url)
    except DiagramRenderError as e:
        print(f"Render failed (render gate): {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
