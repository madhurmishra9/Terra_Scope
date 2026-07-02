# Changelog

All notable changes to TerraScope are documented here, newest first. See [README.md](README.md) for the full feature guide.

---

## v2.7.1

Found and fixed while validating v2.7 end-to-end against real cloned repos and a live Ollama instance:

| Fix | Description |
|---|---|
| **🔁 Indexing survives the dev-server reloader** | `uvicorn --reload` was watching the entire project tree, including `data/` — every chunk written during indexing triggered a full server restart, silently killing the indexing job. Reload is now scoped to `backend/` only |
| **🚀 GA Workflow no longer crashes** | `python-hcl2`'s string-mode parser wraps `variable`/`resource`/`output`/`required_providers` blocks in list-wrapped, quote-labeled dicts; four functions in `hcl_tools.py` assumed plain dicts and threw `AttributeError`/`TypeError` on every repo. All four fixed |
| **💬 Repo Chat no longer crashes on an empty collection** | Querying a tag that's mid-indexing (or was interrupted) hit `ChromaDB`'s `n_results=0` rejection as an unhandled `TypeError`. Now returns no results gracefully instead of crashing the request |
| **🔗 Docgen never fabricates a documentation URL** | Typing an acronym or product name outside the small hardcoded slug map fell back to a naive guess (e.g. "Cloud NAT" → `/cloud-nat/docs`, a real 404) that was presented as an "official documentation reference" regardless of whether it existed. Guessed URLs are now verified (`HEAD`/`GET`) before ever being shown; unverifiable guesses are omitted rather than fabricated. Also filters out locale-duplicate and off-topic crawl results |

See [README §17 Troubleshooting](README.md#17-troubleshooting) for the two most common remaining symptoms (indexing progress and LLM response speed) and how to diagnose them.

---

## v2.7

| Feature | Description |
|---------|-------------|
| **📊 Diagram engine replacement** | Architecture diagrams are now parsed from the module's real HCL (`hcl_graph.py`) into Mermaid source, rendered deterministically via Kroki/mmdc, and carry a `publishable` flag — Confluence publish is **blocked** if the diagram didn't render, never silently swapped for a placeholder. Curated modules also get the Mermaid diagram auto-embedded in their README (idempotent, GitHub-native) |
| **✅ Doc verification guards** | Every generated document is checked post-render for structure conformance (every template section present, in order) and content preservation (no dropped or invented resource/input/output/API facts) before it's allowed to publish |
| **⚠️ Escalation as a first-class outcome** | The repair loop now returns a typed `CurationOutcome` — `candidate_ready`, `escalated`, or `failed` — instead of silently shipping whatever survived the round cap. Escalated output is written to a `-ESCALATED` folder and the UI shows a banner with the outstanding issues instead of a false "success" screen |
| **🌐 Bounded documentation crawler** | Google/Terraform-registry research now follows sublinks (domain-allowlisted, depth ≤2, content-hash deduped, disk-cached) instead of fetching one fixed page — broader grounding material without an unbounded crawl |
| **🛡️ Policy-as-code gate (checkov)** | A new validator layer runs checkov after `validate`/`tflint` pass, with org-approved suppressions configurable via `terrascope.config.yaml`'s `policy.skip_checks` |
| **🧪 `terraform test` gate** | Curated modules now include a deterministically generated `tests/defaults.tftest.hcl` smoke test (mocked provider, `command = plan`, output assertions), executed as the final validation layer |
| **🧹 Proxy-safety hygiene** | New `backend/http_clients.py` (`local_client()` / `external_client()`) makes proxy trust explicit per destination — fixes a real bug where Ollama calls weren't proxy-bypassed like the rest of the local-LLM traffic |

---

## v2.6

| Feature | Description |
|---------|-------------|
| **📐 Schema-grounded generation** | Before Pass A, TerraScope plans the resource types the module needs and validates them against the real provider schema — hallucinated resource types are dropped, and a compact "authoritative schema" block (required args + types, optional arg names, nested blocks) is injected so the model generates against the actual schema instead of guessing |
| **🧩 Deterministic modular layout** | The LLM emits one HCL body; a brace-depth-aware splitter routes every top-level block to its canonical file (`variables.tf`, `outputs.tf`, `locals.tf`, `data.tf`, `main.tf`, `versions.tf`, `providers.tf`), then `terraform fmt` canonicalises it — the modular structure no longer depends on the model getting file placement right |
| **🔁 Validate→repair loop** | After generation, `terraform validate`/schema/tflint errors are fed back to the model with only the offending files, regenerated, re-split, and re-validated — repeating (up to 3 rounds) until the module passes or stops improving |
| **🧹 tflint validation (Layer 5)** | A new tflint layer catches deprecated arguments, invalid enum values, and provider-rule violations that `terraform validate` misses, via a shipped provider-aware `.tflint.hcl` (google/aws/azurerm rulesets) |

---

## v2.5

| Feature | Description |
|---------|-------------|
| **🌐 Ask AI (General Chat)** | New default landing tab — ask anything; automatically searches ALL indexed repos, falls back to general LLM knowledge with clear grounded/mixed/general labelling, keeps conversation history |
| **📄 Enhanced Doc-Gen** | Two-phase preview → publish flow with Google official docs enrichment; works for any product even without curation |
| **🔍 Google Docs Fetch** | Pulls product overview, key features, security notes and official URLs from `cloud.google.com` + the Terraform registry; synthesises with local LLM (temperature 0.0) |
| **📋 Rich Default Template** | Full document generated even without a `.docx` template: overview → features → APIs → IAM roles → resources → inputs → outputs → versions → security → official links |
| **🌐 Confluence URLs** | Publish step returns clickable page URLs per doc type |
| **⚡ Single-command startup** | `npm run dev` in `frontend/` starts both the React UI **and** the FastAPI backend automatically via a Vite plugin; set `TERRASCOPE_NO_BACKEND=1` to skip |
| **⚙ In-UI Settings editor** | New **Settings tab** — edit LLM model/URL, Confluence credentials (saved to `.env`), grounding thresholds, and server config directly from the browser; test connections with one click |
| **🔗 Cross-panel settings link** | Any panel that depends on a missing config shows a direct "Configure in Settings" link |
| **🐛 Table rendering fix** | Input/output tables now populate type and description columns (pydantic model fix + real `<table>` HTML instead of pipe-separated text) |

---

## v2.4

| Feature | Description |
|---------|-------------|
| **📄 Confluence Docgen** | After curation, automatically publish HLD, CPSD, Architectural Design, and Highly Confidential Assessment pages to Confluence — cloned from a reference `.docx` template, fields bound from real module metadata, idempotent re-runs update in place |
| **🔑 `.env` for secrets** | Confluence credentials live in `.env` (separate from `terrascope.config.yaml`); Basic or Bearer auth auto-detected |
| **🖼️ Diagram resolution** | Auto-generates architecture diagrams via `terraform graph | dot`; falls back to folder-supplied images or labelled SVG placeholders |
| **🏷️ Idempotency** | `data/docgen_manifest.json` tracks page IDs per product — re-running updates existing pages instead of creating duplicates; pages are also labelled `terrascope:<product>` |
| **🧪 Dry-run mode** | `--dry-run` writes the would-be Confluence XHTML to `output/docgen_dry_run/` for review without any API calls |
| **🖥️ CLI** | `python -m backend.module_curator.docgen.pipeline --product bigquery --module-path output/... --dry-run` |

---

## v2.3

| Feature | Description |
|---------|-------------|
| **🌐 Multi-cloud GA Workflow** | Auto-detects GCP, AWS, or Azure provider from `versions.tf` — no config changes needed |
| **📰 Cloud release note feeds** | Reads AWS What's New RSS, Azure Updates RSS, and GCP release notes per-service |
| **🔁 Incremental updates** | Persists applied changes in `./data/ga_state/{repo}.json`; re-runs skip already-applied changes |
| **⚠️ Breaking change classification** | Detects and labels WHY a change is breaking: `removed`, `renamed`, `type_changed`, `required_now`, `behavior_changed`, `deprecated_removed` |
| **📝 Migration notes** | Each breaking change includes a plain-English migration guide in the PR |
| **🏷️ Provider badge** | GA Workflow tab shows GCP / AWS / Azure provider chip next to version numbers |
| **🔍 Module Troubleshooter** | New tab — detect logical and syntactic bugs in any module at any Git tag, with safe upgrade path |

---

## v2.0

| Feature | Description |
|---------|-------------|
| **🔧 Module Curation** | Generate complete Terraform modules via LLM Q&A — from a service name, a document, or an existing module |
| **📄 Document-based generation** | Upload a PDF, Word (.docx), or text spec and generate a module from it |
| **🐙 GitHub / local / ZIP source** | Load any existing Terraform module from GitHub URL, local path, ZIP archive, or direct `.tf` upload |
| **🏷️ Self-curation with new tag** | Modify an indexed repo with LLM assistance and create a new Git tag automatically |
| **🌐 Multi-cloud** | GCP (`google`), AWS (`aws`), Azure (`azurerm`) — full provider support |
| **📚 Registry doc fetching** | Scrapes and caches Terraform provider docs locally; smart offline/online detection |
| **🔗 Cross-module references** | Resolves `module {}` sources from ChromaDB + Terraform Registry when generating code |
| **⚡ 3-pass LLM generation** | Each file group gets a dedicated LLM call with a full token budget: Pass A → `main.tf`, Pass B → `variables.tf` + `outputs.tf`, Pass C → `versions.tf` + `README.md` + examples |
| **📁 7-file complete module** | Every curation produces: `main.tf`, `variables.tf`, `outputs.tf`, `versions.tf`, `README.md`, `examples/complete/main.tf`, `terraform.tfvars.example` |
| **🎯 Provider-specific Q&A** | 7 targeted questions per provider (GCP / AWS / Azure) covering workload, networking, IAM, encryption, HA, tagging, and cross-service integrations |
