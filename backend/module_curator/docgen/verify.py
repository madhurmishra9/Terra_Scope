"""
verify.py — Doc verification guards (Priority 2): nothing renders, then just
ships. Two deterministic checks run between render and publish, in the spirit
of the Readmint set-based guard:

1. STRUCTURE conformance -- the generated document MUST contain every section
   the template/IR specifies (by heading). Missing/out-of-order sections are
   reported; the LLM does not get to "improvise" the shape.

2. CONTENT preservation -- every source fact key (resource type, input/output
   name, required API) that SHOULD be covered must appear in the rendered
   doc, and no fact may appear in the doc that isn't traceable to a source.
   Implemented as set operations over normalized fact keys.

Both guards are pure Python / regex — no LLM involved, so they're the same
kind of deterministic check as `terraform validate` for code.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
_HTML_HEADING_RE = re.compile(r"<h[1-6][^>]*>(.*?)</h[1-6]>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")

# Structured-claim pattern: snake_case identifiers (resource types, input/
# output names), *.googleapis.com API names, and roles/* IAM roles. Free
# prose in narrative sections never matches this, so scanning table cells
# with this filter naturally excludes LLM-authored prose from the
# invented-fact check (see check_content docstring).
_STRUCTURED_TOKEN_RE = re.compile(
    r"^[a-z][a-z0-9_]{2,}$"                 # snake_case identifier
    r"|^[a-z0-9.-]+\.googleapis\.com$"      # API name
    r"|^roles/[a-z0-9_.]+$"                 # IAM role
)


def _strip_tags(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text)).strip()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


# --------------------------------------------------------------------------- #
# Structure conformance
# --------------------------------------------------------------------------- #
@dataclass
class TemplateSpec:
    """The required-section set. Sections = required headings, in order."""

    required_sections: list[str]
    ordered: bool = True

    @classmethod
    def from_markdown(cls, template_md: str) -> "TemplateSpec":
        sections = [m.group(2).strip() for m in _MD_HEADING_RE.finditer(template_md)]
        return cls(required_sections=sections)

    @classmethod
    def from_headings(cls, headings: list[str]) -> "TemplateSpec":
        """Build directly from a DocumentIR's HeadingNode texts — the natural
        source of truth in this repo, since templates are .docx (parsed into
        an IR), not markdown files."""
        return cls(required_sections=list(headings))


@dataclass
class ConformanceReport:
    missing_sections: list[str] = field(default_factory=list)
    out_of_order: bool = False
    dropped_facts: list[str] = field(default_factory=list)
    invented_facts: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.missing_sections or self.out_of_order
                    or self.dropped_facts or self.invented_facts)


def _extract_doc_headings(doc: str) -> list[str]:
    """Headings from either markdown (# ...) or rendered HTML (<h1..6>)."""
    html_hits = [_strip_tags(h) for h in _HTML_HEADING_RE.findall(doc)]
    if html_hits:
        return html_hits
    return [m.group(2).strip() for m in _MD_HEADING_RE.finditer(doc)]


def check_structure(doc: str, template: TemplateSpec) -> ConformanceReport:
    """Verify every required section is present (and, if `ordered`, in order).
    `doc` may be markdown or the rendered Confluence-storage-format XHTML."""
    doc_sections = [_norm(s) for s in _extract_doc_headings(doc)]
    required = [_norm(s) for s in template.required_sections]

    missing = [orig for orig, n in zip(template.required_sections, required)
               if n not in doc_sections]

    out_of_order = False
    if template.ordered and not missing:
        positions = [doc_sections.index(n) for n in required]
        out_of_order = positions != sorted(positions)

    return ConformanceReport(missing_sections=missing, out_of_order=out_of_order)


# --------------------------------------------------------------------------- #
# Content preservation
# --------------------------------------------------------------------------- #
def check_content(
    source_fact_keys: set[str],
    doc_fact_keys: set[str],
    required_keys: set[str] | None = None,
) -> ConformanceReport:
    """`*_fact_keys` are normalized identifiers (schema attribute names,
    resource names, API/role names) extracted deterministically from source
    and from the produced doc. required_keys defaults to all source keys.

    Restricted to STRUCTURED claims by design (see extract_covered_keys): the
    LLM-written prose sections legitimately use words outside this key set,
    so this guard only ever compares resource types / input-output names /
    API names / IAM roles — never free narrative text.
    """
    required = required_keys if required_keys is not None else source_fact_keys
    dropped = sorted(required - doc_fact_keys)          # should be covered, isn't
    invented = sorted(doc_fact_keys - source_fact_keys)  # in doc, not in any source
    return ConformanceReport(dropped_facts=dropped, invented_facts=invented)


def extract_covered_keys(rendered_doc: str) -> set[str]:
    """Deterministically scan the rendered doc's TABLE CELLS for structured
    fact keys (resource types, input/output names, API names, IAM roles).

    Tables in this pipeline are bound directly from TFModuleMetadata by
    `template/fields.py` — the LLM never touches them — so scanning only
    `<td>`/markdown-table cells naturally isolates structured, sourced facts
    from the LLM-authored narrative sections (Overview, Key Features, ...),
    satisfying the "restrict invented-fact checks to structured claims" rule
    without needing a separate NLP extraction pass.
    """
    keys: set[str] = set()

    def _consider(raw: str) -> None:
        text = _norm(raw).lstrip("•-*").strip()
        if _STRUCTURED_TOKEN_RE.match(text):
            keys.add(text)

    # Confluence storage-format tables: only the FIRST <td> per row (the
    # "Name" column in required_inputs_table/optional_inputs_table/
    # outputs_table) is an identifier — later columns (Type, Description)
    # hold prose/type words like "string" that would otherwise false-positive
    # as "invented facts". <th> header rows have no <td> and are naturally skipped.
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", rendered_doc, re.IGNORECASE | re.DOTALL):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.IGNORECASE | re.DOTALL)
        if cells:
            _consider(_strip_tags(cells[0]))

    # Bullet-list fields (resource_types_list, apis_required_list, ...): one
    # <p>• item<br/>• item</p> block per FieldNode — split on <br/> so each
    # list item is checked individually against the structured-token filter.
    # Strip whole <table> blocks first — every cell in them (identifier AND
    # Type/Description columns) is already handled by the row/cell scan
    # above; re-scanning their nested <p> tags here would re-admit
    # Type-column words like "string" through the back door.
    body_only = re.sub(r"<table[^>]*>.*?</table>", "", rendered_doc, flags=re.IGNORECASE | re.DOTALL)
    for para in re.findall(r"<p[^>]*>(.*?)</p>", body_only, re.IGNORECASE | re.DOTALL):
        for line in re.split(r"<br\s*/?>", para, flags=re.IGNORECASE):
            _consider(_strip_tags(line))

    # Markdown-style pipe tables (dry-run / non-rendered previews): first
    # cell only, same identifier-column convention as above. Skip the
    # "| --- | --- |" separator row.
    for line in rendered_doc.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            first = stripped.strip("|").split("|")[0].strip()
            if first and set(first) != {"-"}:
                _consider(first)

    return keys
