# TerraScope Doc-Gen Enhancement — Plan & Changelog

## Goal

Turn the doc-gen feature from a TF-metadata-only Confluence publisher into a
**product documentation generator** that:

1. Fetches Google's **official documentation** for the product.
2. Uses the **templates** in `templates/` (and a rich built-in default when no
   `.docx` exists) to lay out a full document.
3. Fills **every available template field** with product-specific information.
4. **Saves locally first** for verification (dry-run → HTML).
5. **Publishes to Confluence** as a deliberate second step and returns the
   **page URL(s)**.
6. Is reachable from a **dedicated UI button** — even for an **un-curated** product.

---

## Architecture (two-phase flow)

```
                    ┌─────────────────────────────────────────────┐
  Product name  ──► │ ① PREVIEW  (POST /api/docgen/preview)        │
  (+ optional        │   • fetch_gcp_docs() → overview/features/    │
   module path)      │     security/APIs/roles/official URLs        │
                     │   • extract TF metadata (if module path)     │
                     │   • bind into template (rich default IR)     │
                     │   • render → HTML, write to dry_run_dir      │
                     │   • RETURN rendered HTML + local paths       │
                     └─────────────────────────────────────────────┘
                                      │  (user verifies in UI)
                                      ▼
                     ┌─────────────────────────────────────────────┐
                     │ ② PUBLISH  (POST /api/docgen/publish)        │
                     │   • same build, dry_run=False                │
                     │   • create/update Confluence pages           │
                     │   • RETURN page_url per doc type             │
                     └─────────────────────────────────────────────┘
```

The two phases share one `DocgenRequest`; only the `dry_run` flag differs, so the
preview is a faithful representation of what gets published.

---

## New source of truth: `gcp_docs_fetcher.py`

Gathers material from three best-effort sources, then synthesises the narrative
sections with the local LLM (temperature 0.0, strictly grounded):

| Source | What it provides | Offline? |
|---|---|---|
| `terrascope.config.yaml` `gcp_products` | APIs, IAM roles | ✓ always |
| Terraform registry (`registry_api`) | resource-level docs | needs network |
| `cloud.google.com/<slug>/docs` | product overview prose | needs network |

The LLM turns the raw material into `overview`, `key_features`, and
`security_considerations`. If the LLM or network is unavailable, a deterministic
fallback keeps the document populated (never blank).

---

## Files changed

### New
- **`backend/module_curator/docgen/gcp_docs_fetcher.py`** — official-docs fetch +
  LLM synthesis. Returns a `GCPDocsBundle`.

### Modified — backend
- **`metadata/extractor.py`** — `TFModuleMetadata` gains `overview`,
  `key_features`, `security_considerations`, `apis_required`, `common_roles`,
  `official_doc_urls`, plus `merge_docs_bundle()`.
- **`pipeline.py`** —
  - `run()` calls `fetch_gcp_docs()` and merges into metadata.
  - `DocgenRequest` gains `fetch_docs` and `provider`.
  - `DocgenResult`/`PageResult` gain `page_url`, `parent_page_url`,
    `official_doc_urls`, `sources_used`, `dry_run_html`.
  - `_minimal_ir()` replaced by a **rich default document** using every
    field-map token (overview → features → APIs → roles → resources → inputs →
    outputs → versions → security → official links).
- **`confluence/client.py`** — added `create_page_full()` / `update_page_full()`
  returning `(id, url)` and a `_page_url()` helper.
- **`template/fields.py`** — **bug fix**: table formatter now handles Pydantic
  models (input/output tables were rendering empty); added `_clean()` to strip
  HCL quote/interpolation artefacts.
- **`render/storage_format.py`** — markdown-style table field values now render
  as real Confluence `<table>` elements instead of flat paragraphs.
- **`templates/example/field_map.yaml`** — new tokens: `product_overview`,
  `key_features_list`, `security_considerations_list`, `apis_required_list`,
  `common_roles_list`, `official_docs_links`.
- **`main.py`** — new routes `POST /api/docgen/preview` and
  `POST /api/docgen/publish` (the existing `/curate/{id}/docgen` and
  `/docgen/run` still work).

### Modified — frontend
- **`frontend/src/App.jsx`** — new **`📄 Docs`** tab and self-contained
  `DocsPanel`: product name, provider, optional module path, "fetch Google docs"
  toggle, doc-type checkboxes, **① Generate Preview** (renders each doc locally
  in a white page view), **② Publish to Confluence** (enabled only after a
  preview and only when `.env` is configured), and clickable result URLs.

---

## Setup unchanged

Publishing still needs the Confluence `.env` keys (`CONFLUENCE_BASE_URL`,
`CONFLUENCE_API_TOKEN`, optional `CONFLUENCE_EMAIL`, `CONFLUENCE_SPACE_KEY`,
`CONFLUENCE_PARENT_TITLE`). The UI shows config status and disables Publish until
they are present. Preview needs none of this.

---

## Verification performed
- `py_compile` on every changed backend module — clean.
- esbuild bundle of `App.jsx` — no JSX/syntax errors.
- End-to-end dry-run for an **un-curated** product (`bigquery`) across all four
  doc types — all `status=ok`, official URL resolved, sections populated.
- End-to-end dry-run **with** a synthetic module dir — input/output/resource
  tables render as real HTML tables with type & description columns filled.

## Notes / follow-ups
- Optional custom `.docx` template: drop `<service>.docx` with `{{ token }}`
  placeholders into `templates/<service>/` plus a `field_map.yaml`; the pipeline
  picks it up automatically and the rich default is only used as a fallback.
- The `cloud.google.com` scrape is best-effort; in a proxied corporate network
  set the proxy env vars so httpx can reach it, otherwise it degrades to config
  metadata + Terraform registry docs.
