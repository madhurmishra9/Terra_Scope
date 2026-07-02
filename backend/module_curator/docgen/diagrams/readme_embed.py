"""
diagrams/readme_embed.py — Put diagrams into READMEs (and other Markdown docs).

Three embed modes:
  * "mermaid" -- a ```mermaid fenced block. GitHub renders this natively, so no
                 image asset is needed. Best default for GitHub-hosted READMEs.
  * "image"   -- a static ![](path) reference to a rendered PNG/SVG. Use for
                 Confluence, the private registry, PDFs -- anywhere mermaid is
                 not rendered.
  * "both"    -- the image reference with the mermaid source kept in a collapsed
                 <details> block, so the picture shows and the source stays
                 editable/diffable.

upsert_diagram_section() is idempotent: it inserts between HTML-comment markers,
so re-running regeneration replaces the diagram in place instead of appending a
duplicate.
"""
from __future__ import annotations

import re

DIAGRAM_START = "<!-- terrascope:diagram:start -->"
DIAGRAM_END = "<!-- terrascope:diagram:end -->"

_MERMAID_BLOCK = re.compile(r"```mermaid\s*\n(.*?)\n?```", re.DOTALL)
_SECTION = re.compile(
    re.escape(DIAGRAM_START) + r".*?" + re.escape(DIAGRAM_END),
    re.DOTALL,
)


def mermaid_block(source: str) -> str:
    return f"```mermaid\n{source.strip()}\n```"


def diagram_section(
    source: str,
    mode: str = "mermaid",
    image_path: str | None = None,
    title: str = "Architecture",
) -> str:
    """Build the wrapped, marker-delimited section for a README."""
    heading = f"## {title}" if title else ""

    if mode == "mermaid":
        body = mermaid_block(source)
    elif mode == "image":
        if not image_path:
            raise ValueError("mode='image' requires image_path")
        body = f"![{title}]({image_path})"
    elif mode == "both":
        if not image_path:
            raise ValueError("mode='both' requires image_path")
        body = (
            f"![{title}]({image_path})\n\n"
            "<details>\n<summary>Diagram source (mermaid)</summary>\n\n"
            f"{mermaid_block(source)}\n\n</details>"
        )
    else:
        raise ValueError(f"Unknown mode: {mode}")

    inner = "\n\n".join(p for p in (heading, body) if p)
    return f"{DIAGRAM_START}\n{inner}\n{DIAGRAM_END}"


def upsert_diagram_section(readme_md: str, section_md: str) -> str:
    """Replace an existing marked diagram section, or append one if absent.
    Idempotent -- safe to run on every regeneration."""
    if _SECTION.search(readme_md):
        return _SECTION.sub(lambda _: section_md, readme_md)
    sep = "" if readme_md.endswith("\n\n") or not readme_md else "\n\n"
    return f"{readme_md}{sep}{section_md}\n"


def extract_mermaid_blocks(markdown: str) -> list[str]:
    """Pull every mermaid source block out of a README/Markdown doc -- the input
    the manual renderer consumes to produce image assets."""
    return [m.group(1).strip() for m in _MERMAID_BLOCK.finditer(markdown)]
