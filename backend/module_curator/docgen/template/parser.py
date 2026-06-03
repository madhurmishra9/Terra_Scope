"""
template/parser.py — Parse a reference .docx into an ordered DocumentIR.

Walk strategy (Milestone 1)
---------------------------
Iterate doc.element.body children in XML document order, not via the
high-level python-docx paragraph/table lists, so that the interleaved
sequence of headings, tables, and diagrams is preserved exactly.

  w:p  with w:drawing → DiagramNode (image blob extracted via relationship ID)
  w:p  with heading style → HeadingNode
  w:p  that is entirely {{ token }} → FieldNode
  w:p  otherwise → ParagraphNode (run-level bold/italic preserved)
  w:tbl → TableNode (first row = headers)

Caption detection: the element immediately after a drawing paragraph is
checked for the "Caption" style; if found it is consumed and used as the
diagram caption rather than any incidental text in the drawing paragraph.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from docx import Document
from docx.oxml.ns import qn

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

# Matches a whole-paragraph placeholder: {{ token }}
_FIELD_ONLY_RE = re.compile(r"^\s*\{\{([^}]+)\}\}\s*$")

_HEADING_STYLE_MAP: dict[str, int] = {
    "heading1": 1, "heading2": 2, "heading3": 3,
    "heading4": 4, "heading5": 5, "heading6": 6,
    "title": 1, "subtitle": 2,
}

_DIAGRAM_TYPE_MAP: dict[str, str] = {
    "hld":                    "HLD",
    "high level design":      "HLD",
    "architectural":          "Architectural Design",
    "architecture":           "Architectural Design",
    "cpsd":                   "CPSD",
    "cloud product security": "CPSD",
    "highly confidential":    "Highly Confidential Assessment",
    "hca":                    "Highly Confidential Assessment",
}


# ── XML helpers ───────────────────────────────────────────────────────────────

def _para_text(para_el) -> str:
    return "".join(t.text or "" for t in para_el.iter(qn("w:t")))


def _para_runs(para_el) -> list[RunData]:
    runs: list[RunData] = []
    for run_el in para_el.iter(qn("w:r")):
        text = "".join(t.text or "" for t in run_el.iter(qn("w:t")))
        if not text:
            continue
        rpr  = run_el.find(qn("w:rPr"))
        bold   = rpr is not None and rpr.find(qn("w:b"))   is not None
        italic = rpr is not None and rpr.find(qn("w:i"))   is not None
        runs.append(RunData(text=text, bold=bold, italic=italic))
    return runs


def _heading_level(para_el) -> Optional[int]:
    ppr = para_el.find(qn("w:pPr"))
    if ppr is None:
        return None
    ps = ppr.find(qn("w:pStyle"))
    if ps is None:
        return None
    sid = (ps.get(qn("w:val")) or "").lower().replace(" ", "")
    # "Heading1" → "heading1", "Heading 2" → "heading2", etc.
    if sid.startswith("heading") and sid[7:].isdigit():
        return int(sid[7:])
    return _HEADING_STYLE_MAP.get(sid)


def _is_caption_style(para_el) -> bool:
    ppr = para_el.find(qn("w:pPr"))
    if ppr is None:
        return False
    ps = ppr.find(qn("w:pStyle"))
    if ps is None:
        return False
    return "caption" in (ps.get(qn("w:val")) or "").lower()


def _extract_image(doc, drawing_el) -> tuple[Optional[bytes], str]:
    """Return (blob, extension) for the first image in a w:drawing element."""
    ns_a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    ns_r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    blip = drawing_el.find(f".//{{{ns_a}}}blip")
    if blip is None:
        return None, "png"
    r_id = blip.get(f"{{{ns_r}}}embed")
    if not r_id:
        return None, "png"
    try:
        img_part = doc.part.related_parts[r_id]
        blob = img_part.blob
        ct   = getattr(img_part, "content_type", "image/png")
        ext  = ct.split("/")[-1].split("+")[0] if "/" in ct else "png"
        return blob, ext
    except Exception:
        return None, "png"


def _guess_diagram_type(caption: str) -> str:
    low = caption.lower()
    for kw, dtype in _DIAGRAM_TYPE_MAP.items():
        if kw in low:
            return dtype
    return "generic"


# ── Table parsing ─────────────────────────────────────────────────────────────

def _parse_table(tbl_el) -> TableNode:
    rows_data: list[list[str]] = []
    for row_el in tbl_el.findall(f".//{qn('w:tr')}"):
        cells: list[str] = []
        for cell_el in row_el.findall(f".//{qn('w:tc')}"):
            cell_text = "\n".join(
                _para_text(p).strip()
                for p in cell_el.findall(f".//{qn('w:p')}")
                if _para_text(p).strip()
            )
            cells.append(cell_text)
        if cells:
            rows_data.append(cells)
    headers = rows_data[0] if rows_data else []
    rows    = rows_data[1:] if len(rows_data) > 1 else []
    return TableNode(headers=headers, rows=rows)


# ── Main entry point ──────────────────────────────────────────────────────────

def parse_docx(
    path: Path,
    product_name: str = "",
    doc_type: str = "",
) -> DocumentIR:
    """
    Parse a reference .docx and return an ordered DocumentIR.

    The XML body is walked element-by-element (not via the high-level
    python-docx API) so that headings, tables, and diagrams appear in
    their actual document order.
    """
    doc      = Document(str(path))
    body     = doc.element.body
    children = list(body)
    nodes: list[IRNode] = []

    i = 0
    while i < len(children):
        el = children[i]

        if el.tag == qn("w:p"):
            drawing_els = list(el.iter(qn("w:drawing")))
            if drawing_els:
                blob, ext = _extract_image(doc, drawing_els[0])
                # Try to grab a caption from the next sibling
                caption = ""
                if i + 1 < len(children):
                    nxt = children[i + 1]
                    if nxt.tag == qn("w:p") and _is_caption_style(nxt):
                        caption = _para_text(nxt).strip()
                        i += 1   # consume the caption element
                if not caption:
                    caption = _para_text(el).strip() or "Diagram"
                dtype = _guess_diagram_type(caption)
                nodes.append(DiagramNode.from_blob(diagram_type=dtype, caption=caption, blob=blob, ext=ext))
                i += 1
                continue

            text = _para_text(el).strip()
            if not text:
                i += 1
                continue

            lvl = _heading_level(el)
            if lvl is not None:
                nodes.append(HeadingNode(level=lvl, text=text))
                i += 1
                continue

            m = _FIELD_ONLY_RE.match(text)
            if m:
                nodes.append(FieldNode(token=m.group(1).strip()))
                i += 1
                continue

            nodes.append(ParagraphNode(text=text, runs=_para_runs(el)))

        elif el.tag == qn("w:tbl"):
            nodes.append(_parse_table(el))

        i += 1

    # Derive document title from first H1
    title = ""
    for n in nodes:
        if isinstance(n, HeadingNode) and n.level == 1:
            title = n.text
            break

    return DocumentIR(nodes=nodes, doc_type=doc_type, product_name=product_name, title=title)
