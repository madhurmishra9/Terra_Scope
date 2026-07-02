"""
pipeline.py — Full docgen pipeline orchestrator.

Stages
------
  1. Extract TF module metadata (from local dir or in-memory tf_files)
  2. Parse reference .docx → DocumentIR
  3. Bind field values from metadata into the IR
  4. Resolve diagram images (auto-gen / folder / placeholder)
  5. Render IR → Confluence storage format XHTML
  6a. Dry-run: write HTML files to disk
  6b. Publish: create/update Confluence pages + upload attachments (idempotent)

Entry point
-----------
  result = await run(DocgenRequest(...))

CLI / standalone usage (no FastAPI required):
  python -m backend.module_curator.docgen.pipeline \
      --product bigquery \
      --module-path output/bigquery_20250601/ \
      --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from backend.module_curator.docgen.ir import DOC_TYPES, DocumentIR, HeadingNode
from backend.module_curator.docgen.manifest import DocgenManifest
from backend.module_curator.docgen.metadata.extractor import (
    TFModuleMetadata,
    extract_from_files,
    extract_from_path,
)
from backend.module_curator.docgen.template.parser import parse_docx
from backend.module_curator.docgen.template.fields import FieldMap, bind_fields
from backend.module_curator.docgen.diagrams.resolver import resolve as resolve_diagram
from backend.module_curator.docgen.render.storage_format import render as render_xhtml
from backend.module_curator.docgen.verify import (
    TemplateSpec,
    check_content,
    check_structure,
    extract_covered_keys,
)

_MANIFEST_PATH   = Path("data/docgen_manifest.json")
_DEFAULT_TEMPLATE = Path("templates/example/example.docx")
_DEFAULT_FIELD_MAP = Path("templates/example/field_map.yaml")


# ── Request / Result models ───────────────────────────────────────────────────

class DocgenRequest(BaseModel):
    product_name:  str
    module_path:   Optional[str] = None       # Path to generated TF module directory
    tf_files:      dict[str, str] = {}        # In-memory .tf content (alt. to module_path)
    ref_template:  str = str(_DEFAULT_TEMPLATE)
    field_map:     str = str(_DEFAULT_FIELD_MAP)
    doc_types:     list[str] = DOC_TYPES
    assets_dir:    Optional[str] = None       # Folder with per-product diagram images
    dry_run:       bool = False
    dry_run_dir:   str = "output/docgen_dry_run"
    fetch_docs:    bool = True                 # Fetch & synthesise Google official docs
    provider:      str = "google"


class PageResult(BaseModel):
    doc_type:     str
    page_id:      Optional[str] = None
    page_url:     Optional[str] = None
    dry_run_path: Optional[str] = None
    dry_run_html: Optional[str] = None
    status:       str = "ok"        # "ok" | "blocked" | "error"
    error:        Optional[str] = None
    problems:     list[str] = []    # diagram render + structure/content guard findings


class DocgenResult(BaseModel):
    product_name:      str
    space_key:         str = ""
    pages:             list[PageResult] = []
    dry_run:           bool = False
    parent_page_url:   Optional[str] = None
    official_doc_urls: list[str] = []
    sources_used:      list[str] = []

    @property
    def success(self) -> bool:
        return all(p.status == "ok" for p in self.pages)


# ── Main entry point ──────────────────────────────────────────────────────────

async def run(req: DocgenRequest) -> DocgenResult:
    """Execute the full docgen pipeline for one product."""
    # 1. Metadata
    meta = _extract_metadata(req)

    # 1b. Enrich with Google official documentation (best-effort)
    sources_used: list[str] = []
    if req.fetch_docs:
        try:
            from backend.module_curator.docgen.gcp_docs_fetcher import fetch_gcp_docs
            bundle = await fetch_gcp_docs(req.product_name, provider=req.provider)
            meta.merge_docs_bundle(bundle)
            sources_used = bundle.sources_used
        except Exception:
            pass

    # 2. Field map
    fm_path = Path(req.field_map)
    if not fm_path.exists():
        fm_path = _DEFAULT_FIELD_MAP
    field_map: Optional[FieldMap] = FieldMap(fm_path) if fm_path.exists() else None

    # 3. Confluence setup (skip on dry-run)
    client         = None
    space_key      = ""
    settings       = None
    if not req.dry_run:
        from backend.module_curator.docgen.config import get_confluence_settings
        from backend.module_curator.docgen.confluence.client import ConfluenceClient
        from backend.module_curator.docgen.confluence.space import resolve_space
        settings  = get_confluence_settings()
        client    = ConfluenceClient(settings)
        space_key = resolve_space(client, settings.confluence_space_key)

    # 4. Manifest (idempotency)
    manifest = DocgenManifest(_MANIFEST_PATH)
    entry    = manifest.get(req.product_name)

    # 5. Ensure parent page exists
    parent_page_id: Optional[str] = None
    parent_page_url: Optional[str] = None
    if not req.dry_run and client and settings:
        parent_page_id, parent_page_url = _ensure_parent_page(
            client, space_key, req.product_name, entry, settings
        )
        manifest.set(req.product_name, parent_page_id=parent_page_id, space_key=space_key)

    # 6. Generate each doc type
    ref_path   = Path(req.ref_template)
    assets_dir = Path(req.assets_dir) if req.assets_dir else None
    module_dir = Path(req.module_path) if req.module_path else None
    result     = DocgenResult(
        product_name=req.product_name,
        space_key=space_key,
        dry_run=req.dry_run,
        parent_page_url=parent_page_url,
        official_doc_urls=meta.official_doc_urls,
        sources_used=sources_used,
    )

    for doc_type in req.doc_types:
        page_result = _generate_doc(
            doc_type=doc_type,
            meta=meta,
            ref_path=ref_path,
            field_map=field_map,
            client=client,
            space_key=space_key,
            parent_page_id=parent_page_id,
            entry=entry,
            manifest=manifest,
            req=req,
            assets_dir=assets_dir,
            module_dir=module_dir,
        )
        result.pages.append(page_result)

    # 7. Persist manifest
    if not req.dry_run:
        manifest.save()

    return result


# ── Per-document generation ───────────────────────────────────────────────────

def _generate_doc(
    doc_type: str,
    meta: TFModuleMetadata,
    ref_path: Path,
    field_map: Optional[FieldMap],
    client,
    space_key: str,
    parent_page_id: Optional[str],
    entry,
    manifest: DocgenManifest,
    req: DocgenRequest,
    assets_dir: Optional[Path],
    module_dir: Optional[Path],
) -> PageResult:
    try:
        # Parse or build a minimal IR
        ir = (
            parse_docx(ref_path, product_name=meta.service_name, doc_type=doc_type)
            if ref_path.exists()
            else _minimal_ir(meta, doc_type, has_module=bool(module_dir and module_dir.exists()))
        )

        # Bind field values
        if field_map:
            ir = bind_fields(ir, field_map, meta)

        # Resolve diagram images — Priority 1: diagram truth from real HCL, a
        # render gate, and a publishable flag the publish step must respect.
        attachment_map: dict[str, str] = {}
        diagram_blobs:  dict[str, tuple[bytes, str]] = {}
        problems: list[str] = []
        any_diagram_blocked = False

        from backend.module_curator.docgen.ir import DiagramNode
        for node in ir.nodes:
            if isinstance(node, DiagramNode):
                diag = resolve_diagram(
                    doc_type=node.diagram_type,
                    caption=node.caption,
                    product_name=meta.service_name,
                    module_dir=module_dir,
                    assets_dir=assets_dir,
                )
                for p in diag.problems:
                    problems.append(f"{node.diagram_type}: {p}")
                if not diag.publishable:
                    any_diagram_blocked = True
                    continue  # never attach a placeholder/unrendered diagram
                slug  = node.diagram_type.lower().replace(" ", "_")
                fname = f"{meta.service_name}_{slug}.{diag.ext}"
                attachment_map[node.caption] = fname
                diagram_blobs[fname] = (diag.image_bytes, diag.ext)

        # Render to XHTML
        page_html  = render_xhtml(ir, attachment_map=attachment_map if not req.dry_run else None)
        page_title = f"{meta.service_name.title()} — {doc_type}"

        # Priority 2: post-render verification guards — structure + content.
        heading_texts = [n.text for n in ir.nodes if isinstance(n, HeadingNode)]
        template_spec = TemplateSpec.from_headings(heading_texts)
        srep = check_structure(page_html, template_spec)
        problems += [f"missing section: {s}" for s in srep.missing_sections]
        if srep.out_of_order:
            problems.append("sections out of template order")

        def _norm_key(k: str) -> str:
            # extract_from_files' hcl2 string-mode parse can leave a block
            # label's surrounding quotes attached to the key — normalize
            # before comparing against extract_covered_keys' normalized set.
            return k.strip().strip('"').strip().lower()

        source_fact_keys = {
            _norm_key(k) for k in (
                set(meta.required_inputs.keys())
                | set(meta.optional_inputs.keys())
                | set(meta.outputs.keys())
                | set(meta.resource_types)
                | set(meta.apis_required)
            )
        }
        if source_fact_keys:
            crep = check_content(source_fact_keys, extract_covered_keys(page_html))
            problems += [f"dropped fact: {f}" for f in crep.dropped_facts]
            problems += [f"invented fact: {f}" for f in crep.invented_facts]

        blocked = any_diagram_blocked or bool(
            srep.missing_sections or srep.out_of_order
            or (source_fact_keys and (crep.dropped_facts or crep.invented_facts))
        )

        # ── Dry run ───────────────────────────────────────────────────────────
        if req.dry_run:
            # Preview may show anything, including a placeholder + its problems —
            # the curator needs to SEE why a doc would be blocked before publish.
            dry_dir = Path(req.dry_run_dir)
            dry_dir.mkdir(parents=True, exist_ok=True)
            slug     = doc_type.lower().replace(" ", "_")
            out_path = dry_dir / f"{meta.service_name}_{slug}.html"
            full_html = f"<!-- {page_title} -->\n{page_html}\n"
            out_path.write_text(full_html, encoding="utf-8")
            return PageResult(
                doc_type=doc_type,
                dry_run_path=str(out_path),
                dry_run_html=page_html,
                problems=problems,
            )

        # ── Publish gate — never publish a placeholder/unverified doc ───────────
        if blocked:
            return PageResult(
                doc_type=doc_type,
                status="blocked",
                error=f"{doc_type}: blocked — {'; '.join(problems)}",
                problems=problems,
            )

        # ── Publish ───────────────────────────────────────────────────────────
        assert client is not None
        existing_id = entry.pages.get(doc_type) if entry else None

        if existing_id:
            version = client.get_page_version(existing_id)
            page_id, page_url = client.update_page_full(existing_id, page_title, page_html, version)
        else:
            page_id, page_url = client.create_page_full(
                space_key, page_title, page_html, parent_id=parent_page_id
            )

        # Upload diagram attachments (non-fatal on failure)
        for fname, (blob, ext) in diagram_blobs.items():
            mime = "image/svg+xml" if ext == "svg" else f"image/{ext}"
            try:
                client.upload_attachment(page_id, fname, blob, mime)
            except Exception:
                pass

        # Label the page with a product tag for future idempotency detection
        try:
            client.add_label(page_id, f"terrascope:{meta.service_name}")
        except Exception:
            pass

        manifest.set(req.product_name, pages={doc_type: page_id})
        return PageResult(doc_type=doc_type, page_id=page_id, page_url=page_url, problems=problems)

    except Exception as exc:
        return PageResult(doc_type=doc_type, status="error", error=str(exc))


# ── Parent page management ────────────────────────────────────────────────────

def _ensure_parent_page(
    client,
    space_key: str,
    product_name: str,
    entry,
    settings,
) -> tuple[str, str]:
    """Get or create the per-product parent page; return (id, url)."""
    if entry and entry.parent_page_id:
        # We still want a URL; build it from base
        url = f"{settings.confluence_base_url}/pages/viewpage.action?pageId={entry.parent_page_id}"
        return entry.parent_page_id, url

    parent_title = f"{product_name.title()} — TerraScope Documentation"

    # Try to nest under the configured section root
    root_page = client.get_page_by_title(space_key, settings.confluence_parent_title)
    root_id   = root_page["id"] if root_page else None

    existing = client.get_page_by_title(space_key, parent_title)
    if existing:
        pid = str(existing["id"])
        url = f"{settings.confluence_base_url}/pages/viewpage.action?pageId={pid}"
        return pid, url

    body = (
        f"<p>TerraScope-generated documentation for the "
        f"<strong>{product_name}</strong> Terraform module.</p>"
    )
    return client.create_page_full(space_key, parent_title, body, parent_id=root_id)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_metadata(req: DocgenRequest) -> TFModuleMetadata:
    if req.module_path and Path(req.module_path).exists():
        return extract_from_path(Path(req.module_path), service_name=req.product_name)
    if req.tf_files:
        return extract_from_files(req.tf_files, service_name=req.product_name)
    return TFModuleMetadata(service_name=req.product_name)


def _minimal_ir(meta: TFModuleMetadata, doc_type: str, has_module: bool = False) -> DocumentIR:
    """
    Research-document IR built programmatically when no reference .docx exists.

    Pre-curation mode (has_module=False, the default): a design/research doc
    grounded in Google's official documentation — no module tables.
    Post-curation mode (has_module=True): the same doc plus the module's
    resources/inputs/outputs tables and version constraints.
    """
    from backend.module_curator.docgen.ir import FieldNode, HeadingNode, ParagraphNode
    suffix = doc_type if has_module else f"{doc_type} — Research"
    title = f"{meta.service_name.title()} — {suffix}"
    nodes = [
        HeadingNode(level=1, text=title),

        HeadingNode(level=2, text="Overview"),
        FieldNode(token="product_overview"),

        HeadingNode(level=2, text="Architecture"),
        FieldNode(token="architecture_notes"),

        HeadingNode(level=2, text="Key Features"),
        FieldNode(token="key_features_list"),

        HeadingNode(level=2, text="Use Cases"),
        FieldNode(token="use_cases_list"),

        HeadingNode(level=2, text="Terraform Design Considerations"),
        FieldNode(token="terraform_design_considerations_list"),

        HeadingNode(level=2, text="Required Google Cloud APIs"),
        FieldNode(token="apis_required_list"),

        HeadingNode(level=2, text="IAM Design"),
        FieldNode(token="iam_design_list"),
    ]

    # Module-grounded sections only when a real module was provided —
    # never render empty tables in a pre-curation research doc.
    if has_module:
        nodes += [
            HeadingNode(level=2, text="Terraform Resources Managed"),
            FieldNode(token="resource_types_list"),

            HeadingNode(level=2, text="Required Inputs"),
            FieldNode(token="required_inputs_table"),

            HeadingNode(level=2, text="Optional Inputs"),
            FieldNode(token="optional_inputs_table"),

            HeadingNode(level=2, text="Outputs"),
            FieldNode(token="outputs_table"),

            HeadingNode(level=2, text="Version Constraints"),
            ParagraphNode(text="Terraform: {{terraform_version}} · Provider: {{provider_version}}"),
        ]

    nodes += [
        HeadingNode(level=2, text="Security Considerations"),
        FieldNode(token="security_considerations_list"),

        HeadingNode(level=2, text="Limits & Quotas"),
        FieldNode(token="limits_and_quotas_list"),

        HeadingNode(level=2, text="Cost Considerations"),
        FieldNode(token="cost_notes"),

        HeadingNode(level=2, text="Open Questions for Curation"),
        FieldNode(token="open_questions_list"),

        HeadingNode(level=2, text="Official Documentation References"),
        FieldNode(token="official_docs_links"),
    ]
    return DocumentIR(
        nodes=nodes,
        doc_type=doc_type,
        product_name=meta.service_name,
        title=title,
    )


# ── CLI entry point ───────────────────────────────────────────────────────────

def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="TerraScope docgen — generate Confluence documentation for a TF module"
    )
    parser.add_argument("--product",     required=True, help="Product / service name")
    parser.add_argument("--module-path", help="Path to the generated Terraform module directory")
    parser.add_argument("--template",    default=str(_DEFAULT_TEMPLATE), help="Reference .docx template path")
    parser.add_argument("--field-map",   default=str(_DEFAULT_FIELD_MAP), help="field_map.yaml path")
    parser.add_argument("--doc-types",   nargs="+", default=DOC_TYPES, help="Doc types to generate")
    parser.add_argument("--assets-dir",  help="Folder with pre-made diagram images")
    parser.add_argument("--dry-run",     action="store_true", help="Write HTML locally, no Confluence calls")
    parser.add_argument("--dry-run-dir", default="output/docgen_dry_run", help="Output folder for dry-run HTML")
    args = parser.parse_args()

    req = DocgenRequest(
        product_name=args.product,
        module_path=args.module_path,
        ref_template=args.template,
        field_map=args.field_map,
        doc_types=args.doc_types,
        assets_dir=args.assets_dir,
        dry_run=args.dry_run,
        dry_run_dir=args.dry_run_dir,
    )
    result = asyncio.run(run(req))
    for page in result.pages:
        if page.status == "ok":
            dest = page.dry_run_path or page.page_id
            print(f"  [ok] {page.doc_type}: {dest}")
        elif page.status == "blocked":
            print(f"  [blocked] {page.doc_type}: {page.error}")
        else:
            print(f"  [error] {page.doc_type}: {page.error}")


if __name__ == "__main__":
    _cli()
