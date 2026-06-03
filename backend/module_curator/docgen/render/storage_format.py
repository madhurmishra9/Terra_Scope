"""
render/storage_format.py — Render a DocumentIR to Confluence storage format (XHTML).

Confluence storage format is a specific XHTML dialect with vendor macro tags.
Key constructs used here:

  <h1> … <h6>                      Section headings
  <p>                               Paragraphs  (<strong>/<em> for bold/italic)
  <table><tbody><tr><th>/<td>       Tables
  <ac:image>                        Image attachment reference
    <ri:attachment ri:filename="…"/>
  <br/>                             Line break within a paragraph

attachment_map: {diagram_caption → attachment_filename}
  When provided, DiagramNodes become proper <ac:image> tags.
  When None (dry-run), they render as italicised placeholder text.
"""
from __future__ import annotations

import html
from typing import Optional

from backend.module_curator.docgen.ir import (
    DiagramNode,
    DocumentIR,
    FieldNode,
    HeadingNode,
    IRNode,
    ParagraphNode,
    RunData,
    TableNode,
)


def render(
    ir: DocumentIR,
    attachment_map: Optional[dict[str, str]] = None,
) -> str:
    """
    Render *ir* to a Confluence storage-format HTML string.

    Parameters
    ----------
    ir             : Bound DocumentIR (FieldNode.value should already be set).
    attachment_map : {caption → attachment_filename} for uploaded diagram images.
                     Pass None in dry-run mode — images become placeholder text.
    """
    att = attachment_map or {}
    parts = [_render_node(node, att) for node in ir.nodes]
    return "\n".join(p for p in parts if p)


# ── Node renderers ────────────────────────────────────────────────────────────

def _render_node(node: IRNode, att: dict[str, str]) -> str:
    if isinstance(node, HeadingNode):
        return _heading(node)
    if isinstance(node, ParagraphNode):
        return _paragraph(node)
    if isinstance(node, TableNode):
        return _table(node)
    if isinstance(node, DiagramNode):
        return _diagram(node, att)
    if isinstance(node, FieldNode):
        return _field(node)
    return ""


def _heading(node: HeadingNode) -> str:
    lvl = max(1, min(6, node.level))
    return f"<h{lvl}>{html.escape(node.text)}</h{lvl}>"


def _paragraph(node: ParagraphNode) -> str:
    if node.runs:
        inner = _render_runs(node.runs)
    else:
        inner = html.escape(node.text)
    inner = inner.replace("\n", "<br/>")
    return f"<p>{inner}</p>"


def _render_runs(runs: list[RunData]) -> str:
    parts: list[str] = []
    for r in runs:
        t = html.escape(r.text)
        if r.bold:
            t = f"<strong>{t}</strong>"
        if r.italic:
            t = f"<em>{t}</em>"
        parts.append(t)
    return "".join(parts)


def _table(node: TableNode) -> str:
    rows: list[str] = []
    if node.headers:
        header_cells = "".join(
            f"<th><p>{html.escape(h)}</p></th>" for h in node.headers
        )
        rows.append(f"<tr>{header_cells}</tr>")
    for row in node.rows:
        data_cells = "".join(
            f"<td><p>{html.escape(c)}</p></td>" for c in row
        )
        rows.append(f"<tr>{data_cells}</tr>")
    body = "\n".join(rows)
    return f"<table><tbody>\n{body}\n</tbody></table>"


def _diagram(node: DiagramNode, att: dict[str, str]) -> str:
    filename = att.get(node.caption)
    caption_html = f"<p><em>{html.escape(node.caption)}</em></p>"
    if not filename:
        return (
            f'<p><em>[Diagram: {html.escape(node.caption)}]</em></p>'
        )
    return (
        f'<ac:image ac:align="center">'
        f'<ri:attachment ri:filename="{html.escape(filename)}"/>'
        f'</ac:image>'
        f'{caption_html}'
    )


def _field(node: FieldNode) -> str:
    value = node.value if node.value is not None else f"[TODO: {node.token}]"
    # Multi-line values (bullet lists, markdown tables) — preserve newlines
    escaped = html.escape(value).replace("\n", "<br/>")
    return f"<p>{escaped}</p>"
