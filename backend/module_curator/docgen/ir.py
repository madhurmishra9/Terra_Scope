"""
ir.py — Intermediate Representation (IR) for documentation generation.

The IR is an ordered list of typed nodes produced by parsing a reference .docx.
It is the stable contract between the template parser, field binder, and renderer.

Node types
----------
  HeadingNode   — section heading at a given level
  ParagraphNode — block of text (may contain inline {{ tokens }})
  TableNode     — tabular data (first row treated as header)
  DiagramNode   — embedded image / diagram with caption and image bytes
  FieldNode     — a whole-paragraph {{ token }} placeholder; carries bound value
"""
from __future__ import annotations

import base64
from enum import Enum
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field


class NodeKind(str, Enum):
    HEADING   = "heading"
    PARAGRAPH = "paragraph"
    TABLE     = "table"
    DIAGRAM   = "diagram"
    FIELD     = "field"


class HeadingNode(BaseModel):
    kind:  Literal["heading"] = "heading"
    level: int                # 1–9
    text:  str


class RunData(BaseModel):
    text:   str
    bold:   bool = False
    italic: bool = False


class ParagraphNode(BaseModel):
    kind: Literal["paragraph"] = "paragraph"
    text: str
    runs: list[RunData] = []


class TableNode(BaseModel):
    kind:    Literal["table"] = "table"
    headers: list[str]
    rows:    list[list[str]]


class DiagramNode(BaseModel):
    kind:         Literal["diagram"] = "diagram"
    diagram_type: str        # "HLD" | "CPSD" | "Architectural Design" | "Highly Confidential Assessment" | "generic"
    caption:      str
    image_b64:    Optional[str] = None   # base64-encoded image bytes (JSON-safe)
    image_ext:    str = "png"

    @classmethod
    def from_blob(
        cls,
        diagram_type: str,
        caption: str,
        blob: Optional[bytes],
        ext: str = "png",
    ) -> "DiagramNode":
        return cls(
            diagram_type=diagram_type,
            caption=caption,
            image_b64=base64.b64encode(blob).decode() if blob else None,
            image_ext=ext,
        )

    def to_blob(self) -> Optional[bytes]:
        if self.image_b64 is None:
            return None
        return base64.b64decode(self.image_b64)


class FieldNode(BaseModel):
    kind:  Literal["field"] = "field"
    token: str               # the placeholder key, e.g. "product_name"
    value: Optional[str] = None   # None until bind_fields() has run


IRNode = Annotated[
    Union[HeadingNode, ParagraphNode, TableNode, DiagramNode, FieldNode],
    Field(discriminator="kind"),
]

DOC_TYPES = [
    "HLD",
    "CPSD",
    "Architectural Design",
    "Highly Confidential Assessment",
]


class DocumentIR(BaseModel):
    nodes:        list[IRNode] = []
    doc_type:     str = ""
    product_name: str = ""
    title:        str = ""

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)

    @classmethod
    def from_json(cls, text: str) -> "DocumentIR":
        return cls.model_validate_json(text)
