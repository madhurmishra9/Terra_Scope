"""
create_example_template.py — Generate templates/example/example.docx

Run once from the repo root:
  python templates/create_example_template.py

Produces a reference Word document whose section structure and {{ token }}
placeholders mirror the four document types (HLD, CPSD, Architectural Design,
Highly Confidential Assessment).  Copy this file to a product-specific folder
(templates/<product>/) and annotate it with additional {{ tokens }} as needed.
"""
from pathlib import Path

from docx import Document
from docx.shared import Pt, RGBColor
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import lxml.etree as etree


# ── Caption style helper ──────────────────────────────────────────────────────

def _set_para_style_direct(para, style_name: str) -> None:
    """Apply a paragraph style by name (creates inline pPr if style missing)."""
    try:
        para.style = para._element.getroottree().getroot().find(
            f".//{qn('w:style')}[@{qn('w:styleId')}='{style_name}']"
        )
    except Exception:
        pass


# ── Document sections ─────────────────────────────────────────────────────────

def _add_heading(doc: Document, text: str, level: int) -> None:
    doc.add_heading(text, level=level)


def _add_para(doc: Document, text: str) -> None:
    doc.add_paragraph(text)


def _add_field(doc: Document, token: str) -> None:
    """Add a whole-paragraph {{ token }} placeholder."""
    doc.add_paragraph(f"{{{{ {token} }}}}")


def _add_table(doc: Document, headers: list[str], example_row: list[str]) -> None:
    tbl = doc.add_table(rows=2, cols=len(headers))
    tbl.style = "Table Grid"
    for i, h in enumerate(headers):
        tbl.rows[0].cells[i].text = h
    for i, v in enumerate(example_row):
        tbl.rows[1].cells[i].text = v


def _add_diagram_placeholder(doc: Document, caption: str) -> None:
    """Add a grey paragraph that the parser treats as a diagram placeholder."""
    p = doc.add_paragraph(f"[Diagram: {caption}]")
    p.style = "Caption"


# ── Section builders ──────────────────────────────────────────────────────────

def _section_overview(doc: Document) -> None:
    _add_heading(doc, "1  Overview", level=1)
    _add_field(doc, "product_name")
    _add_para(doc,
        "This document provides the {{ doc_type }} for the {{ product_name }} "
        "Terraform module on {{ provider_name }}."
    )
    _add_field(doc, "module_description")


def _section_architecture(doc: Document) -> None:
    _add_heading(doc, "2  Architecture", level=1)
    _add_heading(doc, "2.1  High-Level Architecture", level=2)
    _add_diagram_placeholder(doc, "HLD — High Level Design")
    _add_heading(doc, "2.2  Detailed Architecture", level=2)
    _add_diagram_placeholder(doc, "Architectural Design")
    _add_heading(doc, "2.3  Resource Types", level=2)
    _add_field(doc, "resource_types_list")
    _add_heading(doc, "2.4  Module Dependencies", level=2)
    _add_field(doc, "module_calls_list")


def _section_configuration(doc: Document) -> None:
    _add_heading(doc, "3  Configuration", level=1)
    _add_heading(doc, "3.1  Version Constraints", level=2)
    _add_para(doc,
        "Terraform version: {{ terraform_version }}  ·  "
        "Provider version: {{ provider_version }}"
    )
    _add_heading(doc, "3.2  Required Inputs", level=2)
    _add_table(doc,
        headers=["Name", "Type", "Description"],
        example_row=["project_id", "string", "GCP project identifier"],
    )
    _add_field(doc, "required_inputs_table")
    _add_heading(doc, "3.3  Optional Inputs", level=2)
    _add_field(doc, "optional_inputs_table")
    _add_heading(doc, "3.4  Outputs", level=2)
    _add_field(doc, "outputs_table")


def _section_security(doc: Document) -> None:
    _add_heading(doc, "4  Security Design", level=1)
    _add_heading(doc, "4.1  CPSD Diagram", level=2)
    _add_diagram_placeholder(doc, "CPSD — Cloud Product Security Design")
    _add_heading(doc, "4.2  Security Controls", level=2)
    _add_para(doc,
        "This section describes security controls applied by the "
        "{{ product_name }} Terraform module."
    )
    _add_heading(doc, "4.3  Highly Confidential Assessment", level=2)
    _add_diagram_placeholder(doc, "Highly Confidential Assessment")
    _add_field(doc, "readme_excerpt")


def _section_glossary(doc: Document) -> None:
    _add_heading(doc, "5  Glossary", level=1)
    _add_table(doc,
        headers=["Term", "Definition"],
        example_row=["HLD", "High Level Design — top-level architecture overview"],
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def build_template(out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()

    # Cover title
    title = doc.add_paragraph()
    run = title.add_run("{{ product_name }} — TerraScope Documentation")
    run.bold = True
    run.font.size = Pt(20)

    doc.add_paragraph()   # spacer

    _section_overview(doc)
    _section_architecture(doc)
    _section_configuration(doc)
    _section_security(doc)
    _section_glossary(doc)

    doc.save(str(out_path))
    print(f"Written: {out_path}")


if __name__ == "__main__":
    target = Path(__file__).parent / "example" / "example.docx"
    build_template(target)
