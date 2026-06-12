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

    def resolve(self, token: str, meta: Any) -> str:
        """Return the formatted string value for *token* using *meta* data."""
        cfg = self._map.get(token)
        if cfg is None:
            return f"[TODO: {token}]"
        source = cfg.get("source", "")
        fmt    = cfg.get("format", "text")
        raw    = _resolve_source(source, meta, cfg)
        return _format_value(raw, fmt, cfg)

    def tokens(self) -> list[str]:
        return list(self._map.keys())


# ── Source resolution ─────────────────────────────────────────────────────────

def _resolve_source(source: str, meta: Any, cfg: dict) -> Any:
    if source.startswith("literal."):
        return source[len("literal."):]
    if source.startswith("metadata."):
        attr = source[len("metadata."):]
        value = getattr(meta, attr, None)
        if value is None or (isinstance(value, (list, dict)) and not value):
            return cfg.get("fallback", "")
        return value
    return cfg.get("fallback", "")


# ── Value formatting ──────────────────────────────────────────────────────────

def _format_value(value: Any, fmt: str, cfg: dict) -> str:
    if fmt == "bullet_list":
        if isinstance(value, list):
            return "\n".join(f"• {_clean(item)}" for item in value) if value else ""
        return _clean(value)

    if fmt == "table":
        columns = cfg.get("columns", [])
        if isinstance(value, dict) and columns:
            lines = [" | ".join(columns)]
            lines.append(" | ".join(["---"] * len(columns)))
            for key, entry in value.items():
                # Normalise entry to a plain dict (it may be a Pydantic model)
                if hasattr(entry, "model_dump"):
                    entry = entry.model_dump()
                if isinstance(entry, dict):
                    row = []
                    for col in columns:
                        if col == columns[0]:
                            row.append(_clean(key))
                        else:
                            row.append(_clean(entry.get(col, "")))
                    lines.append(" | ".join(row))
                else:
                    row = [_clean(key)] + [""] * (len(columns) - 1)
                    lines.append(" | ".join(row))
            return "\n".join(lines)
        return str(value)

    # Plain text (default)
    if isinstance(value, list):
        return ", ".join(_clean(v) for v in value)
    return _clean(value) if value is not None else ""


def _clean(v: Any) -> str:
    """Stringify and strip surrounding quotes / HCL interpolation artefacts."""
    s = str(v) if v is not None else ""
    s = s.strip()
    # Strip a single pair of surrounding quotes that some HCL parsers leave in
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1]
    # Collapse ${...} interpolation wrappers HCL2 sometimes emits for bare types
    if s.startswith("${") and s.endswith("}"):
        s = s[2:-1]
    return s


# ── IR binding ────────────────────────────────────────────────────────────────

def bind_fields(ir: DocumentIR, field_map: FieldMap, meta: Any) -> DocumentIR:
    """
    Return a new DocumentIR with all {{ token }} occurrences substituted.
    FieldNode.value is set; inline tokens in ParagraphNode/TableNode text
    are replaced via regex substitution.
    """
    new_nodes: list[IRNode] = [_bind_node(n, field_map, meta) for n in ir.nodes]
    return DocumentIR(
        nodes=new_nodes,
        doc_type=ir.doc_type,
        product_name=ir.product_name or getattr(meta, "service_name", ""),
        title=_substitute(ir.title, field_map, meta),
    )


def _bind_node(node: IRNode, fm: FieldMap, meta: Any) -> IRNode:
    if isinstance(node, FieldNode):
        return FieldNode(token=node.token, value=fm.resolve(node.token, meta))

    if isinstance(node, ParagraphNode):
        return ParagraphNode(text=_substitute(node.text, fm, meta), runs=node.runs)

    if isinstance(node, TableNode):
        return TableNode(
            headers=[_substitute(h, fm, meta) for h in node.headers],
            rows=[[_substitute(c, fm, meta) for c in row] for row in node.rows],
        )

    # HeadingNode and DiagramNode pass through unchanged
    return node


def _substitute(text: str, fm: FieldMap, meta: Any) -> str:
    def _replace(m: re.Match) -> str:
        return fm.resolve(m.group(1).strip(), meta)
    return _TOKEN_RE.sub(_replace, text)
