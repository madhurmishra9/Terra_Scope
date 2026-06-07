"""
template/fields.py — Load field_map.yaml and bind metadata values into the IR.

field_map.yaml schema
---------------------
  fields:
    <token>:
      source:   metadata.<attr> | literal.<value>
      format:   text | bullet_list | table          (default: text)
      columns:  [col1, col2, ...]                   (only for format: table)
      fallback: "<default when source is empty>"

After bind_fields() every FieldNode.value is set and every {{ token }}
occurrence inside ParagraphNode.text and TableNode cells is replaced.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from backend.module_curator.docgen.ir import (
    DiagramNode,
    DocumentIR,
    FieldNode,
    HeadingNode,
    IRNode,
    ParagraphNode,
    TableNode,
)

_TOKEN_RE = re.compile(r"\{\{([^}]+)\}\}")


# ── FieldMap ──────────────────────────────────────────────────────────────────

class FieldMap:
    def __init__(self, path: Path) -> None:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        self._map: dict[str, dict] = raw.get("fields", {})

    def resolve(self, token: str, meta: Any, doc_type: str = "") -> str:
        """Return the formatted string value for *token* using *meta* data."""
        cfg = self._map.get(token)
        if cfg is None:
            return f"[TODO: {token}]"
        source = cfg.get("source", "")
        fmt    = cfg.get("format", "text")
        raw    = _resolve_source(source, meta, cfg, doc_type=doc_type)
        return _format_value(raw, fmt, cfg)

    def tokens(self) -> list[str]:
        return list(self._map.keys())


# ── Source resolution ─────────────────────────────────────────────────────────

def _resolve_source(source: str, meta: Any, cfg: dict, doc_type: str = "") -> Any:
    if source.startswith("literal."):
        return source[len("literal."):]
    if source == "context.doc_type":
        return doc_type
    if source.startswith("metadata."):
        attr = source[len("metadata."):]
        value = getattr(meta, attr, None)
        if value is None or (isinstance(value, (list, dict)) and not value):
            return cfg.get("fallback", "")
        return value
    if source.startswith("llm."):
        section_hint = source[len("llm."):]
        from backend.module_curator.docgen.prose.generator import generate_prose
        return generate_prose(section_hint, meta, doc_type)
    return cfg.get("fallback", "")


# ── Value formatting ──────────────────────────────────────────────────────────

def _format_value(value: Any, fmt: str, cfg: dict) -> str:
    if fmt == "bullet_list":
        if isinstance(value, list):
            return "\n".join(f"• {item}" for item in value) if value else ""
        return str(value)

    if fmt == "table":
        columns = cfg.get("columns", [])
        if isinstance(value, dict) and columns:
            lines = [" | ".join(columns)]
            lines.append(" | ".join(["---"] * len(columns)))
            for key, entry in value.items():
                if isinstance(entry, dict):
                    row = []
                    for col in columns:
                        if col == columns[0]:
                            row.append(key)
                        else:
                            row.append(str(entry.get(col, "")))
                    lines.append(" | ".join(row))
                else:
                    row = [key] + [""] * (len(columns) - 1)
                    lines.append(" | ".join(row))
            return "\n".join(lines)
        return str(value)

    # Plain text (default)
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value) if value is not None else ""


# ── IR binding ────────────────────────────────────────────────────────────────

def bind_fields(ir: DocumentIR, field_map: FieldMap, meta: Any) -> DocumentIR:
    """
    Return a new DocumentIR with all {{ token }} occurrences substituted.
    FieldNode.value is set; inline tokens in ParagraphNode/TableNode text
    are replaced via regex substitution. ir.doc_type is forwarded so that
    llm.* sources receive the correct document-type framing.
    """
    doc_type  = ir.doc_type
    new_nodes = [_bind_node(n, field_map, meta, doc_type) for n in ir.nodes]
    return DocumentIR(
        nodes=new_nodes,
        doc_type=doc_type,
        product_name=ir.product_name or getattr(meta, "service_name", ""),
        title=_substitute(ir.title, field_map, meta, doc_type),
    )


def _bind_node(node: IRNode, fm: FieldMap, meta: Any, doc_type: str = "") -> IRNode:
    if isinstance(node, FieldNode):
        return FieldNode(token=node.token, value=fm.resolve(node.token, meta, doc_type))

    if isinstance(node, ParagraphNode):
        from backend.module_curator.docgen.ir import RunData
        new_runs = [
            RunData(text=_substitute(r.text, fm, meta, doc_type), bold=r.bold, italic=r.italic)
            for r in node.runs
        ]
        return ParagraphNode(text=_substitute(node.text, fm, meta, doc_type), runs=new_runs)

    if isinstance(node, TableNode):
        return TableNode(
            headers=[_substitute(h, fm, meta, doc_type) for h in node.headers],
            rows=[[_substitute(c, fm, meta, doc_type) for c in row] for row in node.rows],
        )

    # HeadingNode and DiagramNode pass through unchanged
    return node


def _substitute(text: str, fm: FieldMap, meta: Any, doc_type: str = "") -> str:
    def _replace(m: re.Match) -> str:
        return fm.resolve(m.group(1).strip(), meta, doc_type)
    return _TOKEN_RE.sub(_replace, text)
